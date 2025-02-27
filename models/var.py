import math
from functools import partial
from typing import Optional, Tuple, Union

import torch
import torch.nn as nn
from huggingface_hub import PyTorchModelHubMixin

import dist
from models.basic_var import AdaLNBeforeHead, AdaLNSelfAttn, DAmlp
from models.helpers import gumbel_softmax_with_rng, sample_with_top_k_top_p_
from models.vqvae import VQVAE, VectorQuantizer2
from torch.nn import functional as F
from torch.utils.checkpoint import checkpoint

class SharedAdaLin(nn.Linear):
    def forward(self, cond_BD):
        C = self.weight.shape[0] // 6
        return super().forward(cond_BD).view(-1, 1, 6, C)   # B16C


class VAR(nn.Module):
    def __init__(
        self, vae_local: VQVAE,
        num_classes=1000, depth=16, embed_dim=1024, num_heads=16, mlp_ratio=4., drop_rate=0., attn_drop_rate=0., drop_path_rate=0.,
        norm_eps=1e-6, shared_aln=False, cond_drop_rate=0.1,
        attn_l2_norm=False,
        patch_nums=(1, 2, 3, 4, 5, 6, 8, 10, 13, 16),   # 10 steps by default
        flash_if_available=True, fused_if_available=True,
    ):
        super().__init__()
        # 0. hyperparameters
        assert embed_dim % num_heads == 0
        self.Cvae, self.V = vae_local.Cvae, vae_local.vocab_size
        self.depth, self.C, self.D, self.num_heads = depth, embed_dim, embed_dim, num_heads
        
        self.cond_drop_rate = cond_drop_rate
        self.prog_si = -1   # progressive training
        
        self.patch_nums: Tuple[int] = patch_nums
        self.L = sum(pn ** 2 for pn in self.patch_nums)
        self.first_l = self.patch_nums[0] ** 2
        self.begin_ends = []
        cur = 0
        for i, pn in enumerate(self.patch_nums):
            self.begin_ends.append((cur, cur+pn ** 2))
            cur += pn ** 2
        
        self.num_stages_minus_1 = len(self.patch_nums) - 1
        self.rng = torch.Generator(device=dist.get_device())
        
        # 1. input (word) embedding
        quant: VectorQuantizer2 = vae_local.quantize
        self.vae_proxy: Tuple[VQVAE] = (vae_local,)
        self.vae_quant_proxy: Tuple[VectorQuantizer2] = (quant,)
        self.word_embed = nn.Linear(self.Cvae, self.C)
        
        # 2. class embedding
        init_std = math.sqrt(1 / self.C / 3)
        self.num_classes = num_classes
        self.uniform_prob = torch.full((1, num_classes), fill_value=1.0 / num_classes, dtype=torch.float32, device=dist.get_device())
        self.class_emb = nn.Embedding(self.num_classes + 1, self.C)
        nn.init.trunc_normal_(self.class_emb.weight.data, mean=0, std=init_std)
        self.pos_start = nn.Parameter(torch.empty(1, self.first_l, self.C))
        nn.init.trunc_normal_(self.pos_start.data, mean=0, std=init_std)
        
        # 3. absolute position embedding
        pos_1LC = []
        for i, pn in enumerate(self.patch_nums):
            pe = torch.empty(1, pn*pn, self.C)
            nn.init.trunc_normal_(pe, mean=0, std=init_std)
            pos_1LC.append(pe)
        pos_1LC = torch.cat(pos_1LC, dim=1)     # 1, L, C
        assert tuple(pos_1LC.shape) == (1, self.L, self.C)
        self.pos_1LC = nn.Parameter(pos_1LC)
        
        # level embedding (similar to GPT's segment embedding, used to distinguish different levels of token pyramid)
        self.lvl_embed = nn.Embedding(len(self.patch_nums), self.C)
        nn.init.trunc_normal_(self.lvl_embed.weight.data, mean=0, std=init_std)
        
        # 4. backbone blocks
        self.shared_ada_lin = nn.Sequential(nn.SiLU(inplace=False), SharedAdaLin(self.D, 6*self.C)) if shared_aln else nn.Identity()
        
        norm_layer = partial(nn.LayerNorm, eps=norm_eps)
        self.drop_path_rate = drop_path_rate
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule (linearly increasing)
        self.blocks = nn.ModuleList([
            AdaLNSelfAttn(
                cond_dim=self.D, shared_aln=shared_aln,
                block_idx=block_idx, embed_dim=self.C, norm_layer=norm_layer, num_heads=num_heads, mlp_ratio=mlp_ratio,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[block_idx], last_drop_p=0 if block_idx == 0 else dpr[block_idx-1],
                attn_l2_norm=attn_l2_norm,
                flash_if_available=flash_if_available, fused_if_available=fused_if_available,
            )
            for block_idx in range(depth)
        ])
        
        fused_add_norm_fns = [b.fused_add_norm_fn is not None for b in self.blocks]
        self.using_fused_add_norm_fn = any(fused_add_norm_fns)
        print(
            f'\n[constructor]  ==== flash_if_available={flash_if_available} ({sum(b.attn.using_flash for b in self.blocks)}/{self.depth}), fused_if_available={fused_if_available} (fusing_add_ln={sum(fused_add_norm_fns)}/{self.depth}, fusing_mlp={sum(b.ffn.fused_mlp_func is not None for b in self.blocks)}/{self.depth}) ==== \n'
            f'    [VAR config ] embed_dim={embed_dim}, num_heads={num_heads}, depth={depth}, mlp_ratio={mlp_ratio}\n'
            f'    [drop ratios ] drop_rate={drop_rate}, attn_drop_rate={attn_drop_rate}, drop_path_rate={drop_path_rate:g} ({torch.linspace(0, drop_path_rate, depth)})',
            end='\n\n', flush=True
        )
        
        # 5. attention mask used in training (for masking out the future)
        #    it won't be used in inference, since kv cache is enabled
        d: torch.Tensor = torch.cat([torch.full((pn*pn,), i) for i, pn in enumerate(self.patch_nums)]).view(1, self.L, 1)
        dT = d.transpose(1, 2)    # dT: 11L
        lvl_1L = dT[:, 0].contiguous()
        self.register_buffer('lvl_1L', lvl_1L)
        attn_bias_for_masking = torch.where(d >= dT, 0., -torch.inf).reshape(1, 1, self.L, self.L)
        self.register_buffer('attn_bias_for_masking', attn_bias_for_masking.contiguous())
        
        # 6. classifier head
        self.head_nm = AdaLNBeforeHead(self.C, self.D, norm_layer=norm_layer)
        self.head = nn.Linear(self.C, self.V)


    def get_logits(self, h_or_h_and_residual: Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]], cond_BD: Optional[torch.Tensor]):
        if not isinstance(h_or_h_and_residual, torch.Tensor):
            h, resi = h_or_h_and_residual   # fused_add_norm must be used
            h = resi + self.blocks[-1].drop_path(h)
        else:                               # fused_add_norm is not used
            h = h_or_h_and_residual
        return self.head(self.head_nm(h.float(), cond_BD).float()).float()
    

    @torch.no_grad()
    def autoregressive_infer_cfg(
        self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
        g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
        more_smooth=False,
    ) -> torch.Tensor:   # returns reconstructed image (B, 3, H, W) in [0, 1]
        """
        only used for inference, on autoregressive mode
        :param B: batch size
        :param label_B: imagenet label; if None, randomly sampled
        :param g_seed: random seed
        :param cfg: classifier-free guidance ratio
        :param top_k: top-k sampling
        :param top_p: top-p sampling
        :param more_smooth: smoothing the pred using gumbel softmax; only used in visualization, not used in FID/IS benchmarking
        :return: if returns_vemb: list of embedding h_BChw := vae_embed(idx_Bl), else: list of idx_Bl
        """

        if g_seed is None: rng = None
        else: self.rng.manual_seed(g_seed); rng = self.rng
        
        if label_B is None:
            label_B = torch.multinomial(self.uniform_prob, num_samples=B, replacement=True, generator=rng).reshape(B)
        elif isinstance(label_B, int):
            label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)

        sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))
        
        lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
        next_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]
        
        cur_L = 0
        f_hat = sos.new_zeros(B, self.Cvae, self.patch_nums[-1], self.patch_nums[-1])

        cond_BD_or_gss = self.shared_ada_lin(cond_BD)

        for b in self.blocks: b.attn.kv_caching(True)
        for si, pn in enumerate(self.patch_nums):   # si: i-th segment


            ratio = si / self.num_stages_minus_1
            # last_L = cur_L
            cur_L += pn*pn
            # assert self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].sum() == 0, f'AR with {(self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L] != 0).sum()} / {self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].numel()} mask item'
            x = next_token_map

            AdaLNSelfAttn.forward
            for b in self.blocks:
                x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)

            logits_BlV = self.get_logits(x, cond_BD)

            t = cfg * ratio
            logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]

            idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k, top_p=top_p, num_samples=1)[:, :, 0]

            if not more_smooth: # this is the default case
                h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)   # B, l, Cvae
            else:   # not used when evaluating FID/IS/Precision/Recall
                gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)   # refer to mask-git
                h_BChw = gumbel_softmax_with_rng(logits_BlV.mul(1 + ratio), tau=gum_t, hard=False, dim=-1, rng=rng) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)

            h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)

            f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input(si, len(self.patch_nums), f_hat, h_BChw)

            if si != self.num_stages_minus_1:   # prepare for next stage
                next_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                next_token_map = self.word_embed(next_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
                next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG

 
        for b in self.blocks: b.attn.kv_caching(False)
        # return f_hat
    
        return self.vae_proxy[0].fhat_to_img(f_hat).add_(1).mul_(0.5)   # de-normalize, from [-1, 1] to [0, 1]
    


    def forward(self, label_B: torch.LongTensor, x_BLCv_wo_first_l: torch.Tensor, mask_l=680) -> torch.Tensor:  # returns logits_BLV
        """
        :param label_B: label_B
        :param x_BLCv_wo_first_l: teacher forcing input (B, self.L-self.first_l, self.Cvae)
        :return: logits BLV, V is vocab_size
        """
        bg, ed = self.begin_ends[self.prog_si] if self.prog_si >= 0 else (0, self.L)
        B = x_BLCv_wo_first_l.shape[0]
        with torch.cuda.amp.autocast(enabled=False):

            label_B = torch.where(torch.rand(B, device=label_B.device) < self.cond_drop_rate, self.num_classes, label_B)
            sos = cond_BD = self.class_emb(label_B)
            sos = sos.unsqueeze(1).expand(B, self.first_l, -1) + self.pos_start.expand(B, self.first_l, -1)
            
            if self.prog_si == 0: x_BLC = sos
            else: x_BLC = torch.cat((sos, self.word_embed(x_BLCv_wo_first_l.float())), dim=1)
            x_BLC = x_BLC[:, 0:mask_l]
            x_BLC = x_BLC.contiguous()
            x_BLC = x_BLC + self.lvl_embed(self.lvl_1L[:, 0:mask_l].expand(B, -1)) + self.pos_1LC[:, 0:mask_l] # lvl: BLC;  pos: 1LC
        
        attn_bias = self.attn_bias_for_masking[:, :, 0:mask_l, 0:mask_l]
        cond_BD_or_gss = self.shared_ada_lin(cond_BD)
        
        # hack: get the dtype if mixed precision is used
        temp = x_BLC.new_ones(8, 8)
        main_type = torch.matmul(temp, temp).dtype
        
        x_BLC = x_BLC.to(dtype=main_type)
        cond_BD_or_gss = cond_BD_or_gss.to(dtype=main_type)
        attn_bias = attn_bias.to(dtype=main_type)
        
        AdaLNSelfAttn.forward
        for i, b in enumerate(self.blocks):
            # x_BLC = checkpoint(b, (x_BLC, cond_BD_or_gss, attn_bias))
            x_BLC = b(x=x_BLC, cond_BD=cond_BD_or_gss, attn_bias=attn_bias)
        x_BLC = self.get_logits(x_BLC.float(), cond_BD)
        
        if self.prog_si == 0:
            if isinstance(self.word_embed, nn.Linear):
                x_BLC[0, 0, 0] = x_BLC[0, 0, 0] + self.word_embed.weight[0, 0] * 0 + self.word_embed.bias[0] * 0
            else:
                s = 0
                for p in self.word_embed.parameters():
                    if p.requires_grad:
                        s = s+p.view(-1)[0] * 0
                x_BLC[0, 0, 0] = x_BLC[0, 0, 0]+s
        return x_BLC    # logits BLV, V is vocab_size
    
    def init_weights(self, init_adaln=0.5, init_adaln_gamma=1e-5, init_head=0.02, init_std=0.02, conv_std_or_gain=0.02):
  
        if init_std < 0: init_std = (1 / self.C / 3) ** 0.5     # init_std < 0: automated
        
        print(f'[init_weights] {type(self).__name__} with {init_std=:g}')
        for m in self.modules():
            with_weight = hasattr(m, 'weight') and m.weight is not None
            with_bias = hasattr(m, 'bias') and m.bias is not None
            if isinstance(m, nn.Linear):
                nn.init.trunc_normal_(m.weight.data, std=init_std)
                if with_bias: m.bias.data.zero_()
            elif isinstance(m, nn.Embedding):
                nn.init.trunc_normal_(m.weight.data, std=init_std)
                if m.padding_idx is not None: m.weight.data[m.padding_idx].zero_()
            elif isinstance(m, (nn.LayerNorm, nn.BatchNorm1d, nn.BatchNorm2d, nn.BatchNorm3d, nn.SyncBatchNorm, nn.GroupNorm, nn.InstanceNorm1d, nn.InstanceNorm2d, nn.InstanceNorm3d)):
                if with_weight: m.weight.data.fill_(1.)
                if with_bias: m.bias.data.zero_()
            # conv: VAR has no conv, only VQVAE has conv
            elif isinstance(m, (nn.Conv1d, nn.Conv2d, nn.Conv3d, nn.ConvTranspose1d, nn.ConvTranspose2d, nn.ConvTranspose3d)):
                if conv_std_or_gain > 0: nn.init.trunc_normal_(m.weight.data, std=conv_std_or_gain)
                else: nn.init.xavier_normal_(m.weight.data, gain=-conv_std_or_gain)
                if with_bias: m.bias.data.zero_()
        
        if init_head >= 0:
            if isinstance(self.head, nn.Linear):
                self.head.weight.data.mul_(init_head)
                self.head.bias.data.zero_()
            elif isinstance(self.head, nn.Sequential):
                self.head[-1].weight.data.mul_(init_head)
                self.head[-1].bias.data.zero_()
        
        if isinstance(self.head_nm, AdaLNBeforeHead):
            self.head_nm.ada_lin[-1].weight.data.mul_(init_adaln)
            if hasattr(self.head_nm.ada_lin[-1], 'bias') and self.head_nm.ada_lin[-1].bias is not None:
                self.head_nm.ada_lin[-1].bias.data.zero_()
        
        depth = len(self.blocks)
        for block_idx, sab in enumerate(self.blocks):
            sab: AdaLNSelfAttn
            sab.attn.proj.weight.data.div_(math.sqrt(2 * depth))
            sab.ffn.fc2.weight.data.div_(math.sqrt(2 * depth))
            if hasattr(sab.ffn, 'fcg') and sab.ffn.fcg is not None:
                nn.init.ones_(sab.ffn.fcg.bias)
                nn.init.trunc_normal_(sab.ffn.fcg.weight, std=1e-5)
            if hasattr(sab, 'ada_lin'):
                sab.ada_lin[-1].weight.data[2*self.C:].mul_(init_adaln)
                sab.ada_lin[-1].weight.data[:2*self.C].mul_(init_adaln_gamma)
                if hasattr(sab.ada_lin[-1], 'bias') and sab.ada_lin[-1].bias is not None:
                    sab.ada_lin[-1].bias.data.zero_()
            elif hasattr(sab, 'ada_gss'):
                sab.ada_gss.data[:, :, 2:].mul_(init_adaln)
                sab.ada_gss.data[:, :, :2].mul_(init_adaln_gamma)

        

    def extra_repr(self):
        return f'drop_path_rate={self.drop_path_rate:g}'

#####################################################################################################################

    @torch.no_grad()
    def autoregressive_infer_inpaint(
        self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
        g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
        more_smooth=False, f_hats=None, mask_in=None, mask_out=None
    ) -> torch.Tensor:   # returns reconstructed image (B, 3, H, W) in [0, 1]
        """
        only used for inference, on autoregressive mode
        :param B: batch size
        :param label_B: imagenet label; if None, randomly sampled
        :param g_seed: random seed
        :param cfg: classifier-free guidance ratio
        :param top_k: top-k sampling
        :param top_p: top-p sampling
        :param more_smooth: smoothing the pred using gumbel softmax; only used in visualization, not used in FID/IS benchmarking
        :return: if returns_vemb: list of embedding h_BChw := vae_embed(idx_Bl), else: list of idx_Bl
        """

        if g_seed is None: rng = None
        else: self.rng.manual_seed(g_seed); rng = self.rng
        
        if label_B is None:
            label_B = torch.multinomial(self.uniform_prob, num_samples=B, replacement=True, generator=rng).reshape(B)
        elif isinstance(label_B, int):
            label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)

        sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))
        
        lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
        next_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]
        
        cur_L = 0
        f_hat = sos.new_zeros(B, self.Cvae, self.patch_nums[-1], self.patch_nums[-1])

        cond_BD_or_gss = self.shared_ada_lin(cond_BD)

        mask_in = F.interpolate(mask_in, size=(self.patch_nums[-1],self.patch_nums[-1]), mode='nearest')
        mask_out = F.interpolate(mask_out, size=(self.patch_nums[-1],self.patch_nums[-1]), mode='nearest')

        for b in self.blocks: b.attn.kv_caching(True)
        for si, pn in enumerate(self.patch_nums):   # si: i-th segment


            ratio = si / self.num_stages_minus_1
            # last_L = cur_L
            cur_L += pn*pn
            # assert self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].sum() == 0, f'AR with {(self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L] != 0).sum()} / {self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].numel()} mask item'
            x = next_token_map

            AdaLNSelfAttn.forward
            for b in self.blocks:
                x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)

            logits_BlV = self.get_logits(x, cond_BD)

            t = cfg * ratio
            logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]

            idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k, top_p=top_p, num_samples=1)[:, :, 0]

            if not more_smooth: # this is the default case
                h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)   # B, l, Cvae
            else:   # not used when evaluating FID/IS/Precision/Recall
                gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)   # refer to mask-git
                h_BChw = gumbel_softmax_with_rng(logits_BlV.mul(1 + ratio), tau=gum_t, hard=False, dim=-1, rng=rng) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)

            h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)

            f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input_inpaint(si, len(self.patch_nums), f_hat, h_BChw, f_hats, mask_in, mask_out)

            if si != self.num_stages_minus_1:   # prepare for next stage
                next_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                next_token_map = self.word_embed(next_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
                next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG
 
        for b in self.blocks: b.attn.kv_caching(False)
        return self.vae_proxy[0].fhat_to_img(f_hat).add_(1).mul_(0.5)   # de-normalize, from [-1, 1] to [0, 1]
    

    @torch.no_grad()
    def compute_nll(
        self,
        prefix_tokens: torch.Tensor,   # [B, prefix_length, Cvae] or some shape
        target_tokens: torch.Tensor,   # [B, current_scale_length]
        label_B: torch.LongTensor,
        scale_idx: int = 0
    ) -> float:
        """
        Compute average negative log-likelihood (NLL) for the tokens in `target_tokens`
        given `prefix_tokens`. For example, each 'scale' in your VAR might decode HxW tokens in parallel.
        
        :param prefix_tokens: tokens from previous scales (can be empty if no prefix).
               shape might be [B, prefix_len, Cvae], or [B, prefix_len].
        :param target_tokens: the ground-truth or actual tokens for the current scale.
               shape [B, current_scale_len].
        :param label_B: class labels if class-conditional, shape [B].
        :param scale_idx: index of the scale, if needed for specialized logic (pos embeddings, etc.).
        :return: float scalar = average negative log-likelihood over all tokens in the current scale.
        """
        B = prefix_tokens.shape[0]
        device = prefix_tokens.device
        
        # 1) Convert prefix_tokens to embeddings and add position + level embedding, 
        #    similar to your training forward pass. This is a simplified example:
        #    We have to replicate what you'd do inside forward(...) or autoregressive_infer_cfg(...)
        
        # "sos" or conditional embedding
        label_B_masked = torch.where(
            torch.rand(B, device=device) < self.cond_drop_rate,
            torch.full_like(label_B, self.num_classes),  # unconditional
            label_B
        )
        cond_BD = self.class_emb(label_B_masked)
        # shape: [B, self.C], embedding for class label
        
        # Maybe you'd do something akin to:
        #  - transform prefix tokens with self.word_embed
        #  - add pos embedding, lvl_embed, etc.
        #  - Then concatenate with "sos" for the first scale
        #  This snippet depends on how your code organizes partial scales. 
        #  We'll do a simplified variant:

        # (a) embed prefix tokens
        # If prefix_tokens are already codebook indices: prefix_tokens => self.vae_quant_proxy.embedding(...).
        # If prefix_tokens are quant embeddings: just pass them through self.word_embed(...) if needed.
        
        # for illustration, let's assume prefix_tokens are already shape [B, prefix_len, self.Cvae].
        prefix_embeddings = self.word_embed(prefix_tokens.float())  # -> shape [B, prefix_len, C]
        
        # add position embedding (just assume we have an index offset for scale_idx)
        offset = sum(self.patch_nums[:scale_idx])**2  # start position
        pos_slice = self.pos_1LC[:, offset: offset+prefix_embeddings.shape[1], :]
        lvl_slice = self.lvl_embed(self.lvl_1L[:, offset: offset+prefix_embeddings.shape[1]])
        prefix_embeddings = prefix_embeddings + lvl_slice + pos_slice
        
        # (b) embed the "target_tokens" in teacher-forcing style 
        # same procedure (some shape [B, scale_len, C])
        # this is the chunk we want to do cross-entropy over
        target_embeddings = self.word_embed(self.vae_quant_proxy[0].embedding(target_tokens).transpose(1,2))
        # or if target_tokens are codebook indices, do the same embedding steps.
        tpos_slice = self.pos_1LC[:, offset + prefix_embeddings.shape[1]: offset + prefix_embeddings.shape[1] + target_embeddings.shape[1], :]
        tlvl_slice = self.lvl_embed(self.lvl_1L[:, offset + prefix_embeddings.shape[1]: offset + prefix_embeddings.shape[1] + target_embeddings.shape[1]])
        target_embeddings = target_embeddings + tlvl_slice + tpos_slice
        
        # Now combine everything: [prefix + target]
        full_input = torch.cat([prefix_embeddings, target_embeddings], dim=1) # shape [B, prefix_len+scale_len, C]
        
        attn_bias = self.attn_bias_for_masking[:, :, 0:full_input.shape[1], 0:full_input.shape[1]]
        cond_BD_or_gss = self.shared_ada_lin(cond_BD).to(full_input.dtype)
        
        # 2) Pass through the transformer
        x = full_input
        for blk in self.blocks:
            x = blk(x=x, cond_BD=cond_BD_or_gss, attn_bias=attn_bias)
        
        # 3) get logits
        logits = self.get_logits(x, cond_BD)  # shape [B, (prefix+target_len), V]

        # We only care about the logits for the *target* portion
        # i.e. slice out the last target_len positions
        target_len = target_embeddings.shape[1]
        prefix_len = prefix_embeddings.shape[1]
        relevant_logits = logits[:, prefix_len : prefix_len+target_len, :]  # shape [B, target_len, V]
        
        # 4) compute negative log-likelihood
        # gather the log-prob of each token in target_tokens
        log_probs = F.log_softmax(relevant_logits.float(), dim=-1)  # [B, target_len, V]
        
        # each row is one position, we gather using target_tokens
        # ensure target_tokens shape is [B, target_len]
        # we want index positions for each batch, each position
        batch_idxs = torch.arange(B, device=device).unsqueeze(1).expand(B, target_len)     # [B, target_len]
        pos_idxs = torch.arange(target_len, device=device).unsqueeze(0).expand(B, target_len)
        
        chosen_log_probs = log_probs[batch_idxs, pos_idxs, target_tokens]  # [B, target_len]
        
        # average nll across all B * target_len
        nll = -chosen_log_probs.mean().item()  # scalar float
        return nll

    @torch.no_grad()
    def autoregressive_inpaint_draft(
        self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
        g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
        more_smooth=False, exit_num = 7, f_hats=None, mask_in=None, mask_out=None, temp=None
    ) -> torch.Tensor:   # returns reconstructed image (B, 3, H, W) in [0, 1]
        """
        only used for inference, on autoregressive mode
        :param B: batch size
        :param label_B: imagenet label; if None, randomly sampled
        :param g_seed: random seed
        :param cfg: classifier-free guidance ratio
        :param top_k: top-k sampling
        :param top_p: top-p sampling
        :param more_smooth: smoothing the pred using gumbel softmax; only used in visualization, not used in FID/IS benchmarking
        :return: if returns_vemb: list of embedding h_BChw := vae_embed(idx_Bl), else: list of idx_Bl
        """
        if g_seed is None: rng = None
        else: self.rng.manual_seed(g_seed); rng = self.rng
        
        if label_B is None:
            label_B = torch.multinomial(self.uniform_prob, num_samples=B, replacement=True, generator=rng).reshape(B)
        elif isinstance(label_B, int):
            label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)

        sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))
        
        lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
        next_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]
        
        cur_L = 0
        f_hat = sos.new_zeros(B, self.Cvae, self.patch_nums[-1], self.patch_nums[-1])

        cond_BD_or_gss = self.shared_ada_lin(cond_BD)

        token_hub = []
        logits_hub = []

        mask_in = F.interpolate(mask_in, size=(self.patch_nums[-1],self.patch_nums[-1]), mode='nearest')
        mask_out = F.interpolate(mask_out, size=(self.patch_nums[-1],self.patch_nums[-1]), mode='nearest')
        
        for b in self.blocks: b.attn.kv_caching(True)
        for si, pn in enumerate(self.patch_nums):   # si: i-th segment

            if si == exit_num:
                break
  
            ratio = si / self.num_stages_minus_1
            # last_L = cur_L
            cur_L += pn*pn
            # assert self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].sum() == 0, f'AR with {(self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L] != 0).sum()} / {self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].numel()} mask item'
            x = next_token_map

            AdaLNSelfAttn.forward
            for b in self.blocks:
                x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)

            logits_BlV = self.get_logits(x, cond_BD)
            
            t = cfg * ratio
            logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]

            logits_hub.append(logits_BlV)

            idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k[si], top_p=top_p, num_samples=1,temp=temp[si])[:, :, 0]

            if not more_smooth: # this is the default case
                h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)   # B, l, Cvae
            else:   # not used when evaluating FID/IS/Precision/Recall
                gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)   # refer to mask-git
                h_BChw = gumbel_softmax_with_rng(logits_BlV.mul(1 + ratio), tau=gum_t, hard=False, dim=-1, rng=rng) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)

            h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)

            # f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input(si, len(self.patch_nums), f_hat, h_BChw)
            f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input_inpaint(si, len(self.patch_nums), f_hat, h_BChw, f_hats, mask_in, mask_out)

            # prepare for next stage
            next_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
            token_hub.append(next_token_map)
            next_token_map = self.word_embed(next_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
            next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG

        token_hub = torch.cat(token_hub, dim=1)
        logits_hub = torch.cat(logits_hub, dim=1)
        
        for b in self.blocks: b.attn.kv_caching(False)
        return f_hat, token_hub, logits_hub   # de-normalize, from [-1, 1] to [0, 1]

    @torch.no_grad()
    def autoregressive_inpaint_refine(
            self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
            g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
            more_smooth=False, draft = None, f_hat = None, logits_hub=None, entry_num = 7, f_hats=None, mask_in=None, mask_out=None, temp=None
        ) -> torch.Tensor:   # returns reconstructed image (B, 3, H, W) in [0, 1]
            """
            only used for inference, on autoregressive mode
            :param B: batch size
            :param label_B: imagenet label; if None, randomly sampled
            :param g_seed: random seed
            :param cfg: classifier-free guidance ratio
            :param top_k: top-k sampling
            :param top_p: top-p sampling
            :param more_smooth: smoothing the pred using gumbel softmax; only used in visualization, not used in FID/IS benchmarking
            :return: if returns_vemb: list of embedding h_BChw := vae_embed(idx_Bl), else: list of idx_Bl
            """
            if g_seed is None: rng = None
            else: self.rng.manual_seed(g_seed); rng = self.rng
            
            if label_B is None:
                label_B = torch.multinomial(self.uniform_prob, num_samples=B, replacement=True, generator=rng).reshape(B)
            elif isinstance(label_B, int):
                label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)

            sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))

            token_hub = draft

            lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
            first_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]

            exit_points = [1,5,14,30,55,91,155,255,424,680]
            pindex = exit_points[entry_num]
            next_token_map = token_hub
            next_token_map = self.word_embed(next_token_map) + lvl_pos[:,1:pindex]   
            next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG
            next_token_map = torch.cat([first_token_map,next_token_map],dim=1)

            attn_bias = self.attn_bias_for_masking[:,:,0:pindex,0:pindex]

            cond_BD_or_gss = self.shared_ada_lin(cond_BD)

            mask_in = F.interpolate(mask_in, size=(self.patch_nums[-1],self.patch_nums[-1]), mode='nearest')
            mask_out = F.interpolate(mask_out, size=(self.patch_nums[-1],self.patch_nums[-1]), mode='nearest')

            cur_L = 0

            for b in self.blocks: b.attn.kv_caching(True)

            for si, pn in enumerate(self.patch_nums):   # si: i-th segment
                cur_L += pn*pn
                if si<entry_num:
                    continue
                x = next_token_map
                AdaLNSelfAttn.forward
                if si == entry_num:
                    for b in self.blocks:
                        x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=attn_bias)
                elif si > entry_num:
                    for b in self.blocks:
                        x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)
                
                logits_BlV = self.get_logits(x, cond_BD)

                if si == entry_num:
                    ratio = si / self.num_stages_minus_1
                    t = cfg * ratio 
                    logits_BlV = (1+t) * logits_BlV[:B,cur_L-pn*pn:cur_L] - t * logits_BlV[B:,cur_L-pn*pn:cur_L]
                    logits_BlV = torch.cat([logits_hub,logits_BlV], dim=1)
                    new_L = 0
                    for a, b in enumerate(self.patch_nums[0:entry_num+1]):
                        idx_Bl=sample_with_top_k_top_p_(logits_BlV[:B,new_L:new_L + self.patch_nums[a] ** 2], rng=rng, top_k=top_k[a], top_p=top_p, num_samples=1,temp=temp[a])[:, :, 0]
                        new_L += b*b
 
                    
                elif si > entry_num:
                    ratio = si / self.num_stages_minus_1
                    t = cfg * ratio
                    logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]
                    idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k[si], top_p=top_p, num_samples=1,temp=temp[si])[:, :, 0]


                if not more_smooth: # this is the default case
                    h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)   # B, l, Cvae
                else:   # not used when evaluating FID/IS/Precision/Recall
                    gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)   # refer to mask-git
                    h_BChw = gumbel_softmax_with_rng(logits_BlV.mul(1 + ratio), tau=gum_t, hard=False, dim=-1, rng=rng) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)

                h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)

                # f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input(si, len(self.patch_nums), f_hat, h_BChw)
                f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input_inpaint(si, len(self.patch_nums), f_hat, h_BChw, f_hats, mask_in, mask_out)
                
                if si != self.num_stages_minus_1:   # prepare for next stage
                    next_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                    next_token_map = self.word_embed(next_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
                    next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG
                
            for b in self.blocks: b.attn.kv_caching(False)
                      
            return self.vae_proxy[0].fhat_to_img(f_hat).add_(1).mul_(0.5)   # de-normalize, from [-1, 1] to [0, 1]
    








    @torch.no_grad()
    def autoregressive_infer_cfg_draft(
        self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
        g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
        more_smooth=False, exit_num = 7, temp=None
    ) -> torch.Tensor:   # returns reconstructed image (B, 3, H, W) in [0, 1]
        """
        only used for inference, on autoregressive mode
        :param B: batch size
        :param label_B: imagenet label; if None, randomly sampled
        :param g_seed: random seed
        :param cfg: classifier-free guidance ratio
        :param top_k: top-k sampling
        :param top_p: top-p sampling
        :param more_smooth: smoothing the pred using gumbel softmax; only used in visualization, not used in FID/IS benchmarking
        :return: if returns_vemb: list of embedding h_BChw := vae_embed(idx_Bl), else: list of idx_Bl
        """
        if g_seed is None: rng = None
        else: self.rng.manual_seed(g_seed); rng = self.rng
        
        if label_B is None:
            label_B = torch.multinomial(self.uniform_prob, num_samples=B, replacement=True, generator=rng).reshape(B)
        elif isinstance(label_B, int):
            label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)

        sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))
        
        lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
        next_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]
        
        cur_L = 0
        f_hat = sos.new_zeros(B, self.Cvae, self.patch_nums[-1], self.patch_nums[-1])

        cond_BD_or_gss = self.shared_ada_lin(cond_BD)

        token_hub = []
        
        for b in self.blocks: b.attn.kv_caching(True)
        for si, pn in enumerate(self.patch_nums):   # si: i-th segment

            if si == exit_num:
                break
  
            ratio = si / self.num_stages_minus_1
            # last_L = cur_L
            cur_L += pn*pn
            # assert self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].sum() == 0, f'AR with {(self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L] != 0).sum()} / {self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].numel()} mask item'
            x = next_token_map

            AdaLNSelfAttn.forward
            for b in self.blocks:
                x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)

            logits_BlV = self.get_logits(x, cond_BD)
            
            t = cfg * ratio
            logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]

            idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k[si], top_p=top_p, num_samples=1, temp=temp[si])[:, :, 0]

            if not more_smooth: # this is the default case
                h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)   # B, l, Cvae
            else:   # not used when evaluating FID/IS/Precision/Recall
                gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)   # refer to mask-git
                h_BChw = gumbel_softmax_with_rng(logits_BlV.mul(1 + ratio), tau=gum_t, hard=False, dim=-1, rng=rng) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)

            h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)

            f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input(si, len(self.patch_nums), f_hat, h_BChw)

            # prepare for next stage
            next_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
            token_hub.append(next_token_map)
            next_token_map = self.word_embed(next_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
            next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG

        token_hub = torch.cat(token_hub, dim=1)
        
        for b in self.blocks: b.attn.kv_caching(False)
        return f_hat, token_hub   # de-normalize, from [-1, 1] to [0, 1]

    @torch.no_grad()
    def autoregressive_infer_cfg_refine(
            self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
            g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
            more_smooth=False, draft = None, f_hat = None, entry_num = 7, temp=1
        ) -> torch.Tensor:   # returns reconstructed image (B, 3, H, W) in [0, 1]
            """
            only used for inference, on autoregressive mode
            :param B: batch size
            :param label_B: imagenet label; if None, randomly sampled
            :param g_seed: random seed
            :param cfg: classifier-free guidance ratio
            :param top_k: top-k sampling
            :param top_p: top-p sampling
            :param more_smooth: smoothing the pred using gumbel softmax; only used in visualization, not used in FID/IS benchmarking
            :return: if returns_vemb: list of embedding h_BChw := vae_embed(idx_Bl), else: list of idx_Bl
            """
            if g_seed is None: rng = None
            else: self.rng.manual_seed(g_seed); rng = self.rng
            
            if label_B is None:
                label_B = torch.multinomial(self.uniform_prob, num_samples=B, replacement=True, generator=rng).reshape(B)
            elif isinstance(label_B, int):
                label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)

            sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))

            token_hub = draft

            lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
            first_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]

            exit_points = [1,5,14,30,55,91,155,255,424,680]
            pindex = exit_points[entry_num]
            next_token_map = token_hub
            next_token_map = self.word_embed(next_token_map) + lvl_pos[:,1:pindex]   
            next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG
            next_token_map = torch.cat([first_token_map,next_token_map],dim=1)

            attn_bias = self.attn_bias_for_masking[:,:,0:pindex,0:pindex]

            cond_BD_or_gss = self.shared_ada_lin(cond_BD)

            cur_L = 0

            for b in self.blocks: b.attn.kv_caching(True)

            for si, pn in enumerate(self.patch_nums):   # si: i-th segment
                cur_L += pn*pn
                if si<entry_num:
                    continue
                x = next_token_map
                AdaLNSelfAttn.forward
                if si == entry_num:
                    for b in self.blocks:
                        x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=attn_bias)
                elif si > entry_num:
                    for b in self.blocks:
                        x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)
                
                logits_BlV = self.get_logits(x, cond_BD)

                if si == entry_num:

                    ratio = si / self.num_stages_minus_1
                    t = cfg * ratio 
                    logits_BlV[:B,cur_L-pn*pn:cur_L] = (1+t) * logits_BlV[:B,cur_L-pn*pn:cur_L] - t * logits_BlV[B:,cur_L-pn*pn:cur_L]


                    new_L = 0
                    for a, b in enumerate(self.patch_nums[0:entry_num+1]):
                        idx_Bl=sample_with_top_k_top_p_(logits_BlV[:B,new_L:new_L + self.patch_nums[a] ** 2], rng=rng, top_k=top_k[a], top_p=top_p, num_samples=1, temp=temp[a])[:, :, 0]
                        new_L += b*b

                    logits_BlV = logits_BlV[:B,cur_L-pn*pn:cur_L]
 
                    
                elif si > entry_num:
                    ratio = si / self.num_stages_minus_1
                    t = cfg * ratio
                    logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]
                    idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k[si], top_p=top_p, num_samples=1, temp=temp[si])[:, :, 0]


                if not more_smooth: # this is the default case
                    h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)   # B, l, Cvae
                else:   # not used when evaluating FID/IS/Precision/Recall
                    gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)   # refer to mask-git
                    h_BChw = gumbel_softmax_with_rng(logits_BlV.mul(1 + ratio), tau=gum_t, hard=False, dim=-1, rng=rng) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)

        
                h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)

                f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input(si, len(self.patch_nums), f_hat, h_BChw)
                
                if si != self.num_stages_minus_1:   # prepare for next stage
                    next_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                    next_token_map = self.word_embed(next_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
                    next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG
                
            for b in self.blocks: b.attn.kv_caching(False)
                      
            return self.vae_proxy[0].fhat_to_img(f_hat).add_(1).mul_(0.5)   # de-normalize, from [-1, 1] to [0, 1]
            
    


    @torch.no_grad()
    def autoregressive_infer_cfg_mid(
            self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
            g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
            more_smooth=False, draft = None, f_hat = None, entry_num = 7
        ) -> torch.Tensor:   # returns reconstructed image (B, 3, H, W) in [0, 1]
            """
            only used for inference, on autoregressive mode
            :param B: batch size
            :param label_B: imagenet label; if None, randomly sampled
            :param g_seed: random seed
            :param cfg: classifier-free guidance ratio
            :param top_k: top-k sampling
            :param top_p: top-p sampling
            :param more_smooth: smoothing the pred using gumbel softmax; only used in visualization, not used in FID/IS benchmarking
            :return: if returns_vemb: list of embedding h_BChw := vae_embed(idx_Bl), else: list of idx_Bl
            """
            if g_seed is None: rng = None
            else: self.rng.manual_seed(g_seed); rng = self.rng
            
            if label_B is None:
                label_B = torch.multinomial(self.uniform_prob, num_samples=B, replacement=True, generator=rng).reshape(B)
            elif isinstance(label_B, int):
                label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)

            sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))

            token_hub = draft

            lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
            first_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]

            exit_points = [1,5,14,30,55,91,155,255,424,680]
            pindex = exit_points[entry_num]
            next_token_map = token_hub
            next_token_map = self.word_embed(next_token_map) + lvl_pos[:,1:pindex]   
            next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG
            next_token_map = torch.cat([first_token_map,next_token_map],dim=1)

            attn_bias = self.attn_bias_for_masking[:,:,0:pindex,0:pindex]

            cond_BD_or_gss = self.shared_ada_lin(cond_BD)

            cur_L = 0

            for b in self.blocks: b.attn.kv_caching(True)

            for si, pn in enumerate(self.patch_nums):   # si: i-th segment
                cur_L += pn*pn
                if si>entry_num:
                    break
                if si<entry_num:
                    continue
                x = next_token_map
                AdaLNSelfAttn.forward
                if si == entry_num:
                    for b in self.blocks:
                        x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=attn_bias)
                elif si > entry_num:
                    for b in self.blocks:
                        x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)
                
                logits_BlV = self.get_logits(x, cond_BD)

                if si == entry_num:

                    ratio = si / self.num_stages_minus_1
                    t = cfg * ratio 
                    logits_BlV[:B,cur_L-pn*pn:cur_L] = (1+t) * logits_BlV[:B,cur_L-pn*pn:cur_L] - t * logits_BlV[B:,cur_L-pn*pn:cur_L]


                    new_L = 0
                    for a, b in enumerate(self.patch_nums[0:entry_num+1]):
                        idx_Bl=sample_with_top_k_top_p_(logits_BlV[:B,new_L:new_L + self.patch_nums[a] ** 2], rng=rng, top_k=top_k[a], top_p=top_p, num_samples=1)[:, :, 0]
                        new_L += b*b
 
                    
                elif si > entry_num:
                    ratio = si / self.num_stages_minus_1
                    t = cfg * ratio
                    logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]
                    idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k[si], top_p=top_p, num_samples=1)[:, :, 0]


                if not more_smooth: # this is the default case
                    h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)   # B, l, Cvae
                else:   # not used when evaluating FID/IS/Precision/Recall
                    gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)   # refer to mask-git
                    h_BChw = gumbel_softmax_with_rng(logits_BlV.mul(1 + ratio), tau=gum_t, hard=False, dim=-1, rng=rng) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)

                h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)

                f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input(si, len(self.patch_nums), f_hat, h_BChw)
                
                if si != self.num_stages_minus_1:   # prepare for next stage
                    next_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                    token_hub = torch.cat([token_hub,next_token_map],dim=1)
                    next_token_map = self.word_embed(next_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
                    next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG
                
            for b in self.blocks: b.attn.kv_caching(False)
                      
            return f_hat, token_hub   # de-normalize, from [-1, 1] to [0, 1]
    



    # @torch.no_grad()
    # def autoregressive_infer_cfg_draft_beamsearch(
    #     self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
    #     g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
    #     more_smooth=False, exit_num = 7, temp=1, beamwidth=2
    # ) -> torch.Tensor:   # returns reconstructed image (B, 3, H, W) in [0, 1]
    #     """
    #     only used for inference, on autoregressive mode
    #     :param B: batch size
    #     :param label_B: imagenet label; if None, randomly sampled
    #     :param g_seed: random seed
    #     :param cfg: classifier-free guidance ratio
    #     :param top_k: top-k sampling
    #     :param top_p: top-p sampling
    #     :param more_smooth: smoothing the pred using gumbel softmax; only used in visualization, not used in FID/IS benchmarking
    #     :return: if returns_vemb: list of embedding h_BChw := vae_embed(idx_Bl), else: list of idx_Bl
    #     """
    #     if g_seed is None: rng = None
    #     else: self.rng.manual_seed(g_seed); rng = self.rng
        
    #     if label_B is None:
    #         label_B = torch.multinomial(self.uniform_prob, num_samples=B, replacement=True, generator=rng).reshape(B)
    #     elif isinstance(label_B, int):
    #         label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)

    #     sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))
        
    #     lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
    #     next_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]
        
    #     cur_L = 0
    #     f_hat = sos.new_zeros(B, self.Cvae, self.patch_nums[-1], self.patch_nums[-1])

    #     cond_BD_or_gss = self.shared_ada_lin(cond_BD)
    #     cond_BD_or_gss_ori = cond_BD_or_gss.clone()
    #     cond_BD_ori = cond_BD.clone()

    #     token_hub = []

    #     width = beamwidth

    #     basebatch = B
        
    #     for b in self.blocks: b.attn.kv_caching(True)
    #     for si, pn in enumerate(self.patch_nums):   # si: i-th segment

    #         if si == exit_num:
    #             break
  
    #         ratio = si / self.num_stages_minus_1
    #         # last_L = cur_L
    #         cur_L += pn*pn
    #         # assert self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].sum() == 0, f'AR with {(self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L] != 0).sum()} / {self.attn_bias_for_masking[:, :, last_L:cur_L, :cur_L].numel()} mask item'
    #         x = next_token_map

    #         AdaLNSelfAttn.forward
    #         for b in self.blocks:
    #             x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)

    #         logits_BlV = self.get_logits(x, cond_BD)
            
    #         t = cfg * ratio
    #         logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]

    #         if si<3:
    #             idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k[si], top_p=top_p, num_samples=width, temp=temp).view(B*width,-1)
    #             f_hat = f_hat.repeat(width, 1, 1, 1)
    #             cond_BD_or_gss = cond_BD_or_gss.repeat_interleave(repeats=width, dim=0)
    #             cond_BD = cond_BD.repeat_interleave(repeats=width, dim=0)
    #             B = B*width
    #         elif si==3:
    #             idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k[si], top_p=top_p, num_samples=width, temp=temp).view(basebatch,pn*pn, -1)[:, :, 0]
    #             f_hat = f_hat[0:basebatch, :, :, :]
    #             cond_BD_or_gss = cond_BD_or_gss_ori
    #             cond_BD = cond_BD_ori
    #             B = basebatch
    #         elif si>3:
    #             idx_Bl = sample_with_top_k_top_p_(logits_BlV, rng=rng, top_k=top_k[si], top_p=top_p, num_samples=1, temp=temp)[:, :, 0]
    #         print(si, idx_Bl.shape)

    #         if not more_smooth: # this is the default case
    #             h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)   # B, l, Cvae
    #         else:   # not used when evaluating FID/IS/Precision/Recall
    #             gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)   # refer to mask-git
    #             h_BChw = gumbel_softmax_with_rng(logits_BlV.mul(1 + ratio), tau=gum_t, hard=False, dim=-1, rng=rng) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)

    #         h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)

    #         f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input(si, len(self.patch_nums), f_hat, h_BChw)

    #         # prepare for next stage
    #         next_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
    #         token_hub.append(next_token_map)
    #         next_token_map = self.word_embed(next_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
    #         next_token_map = next_token_map.repeat(2, 1, 1)   # double the batch sizes due to CFG

    #     token_hub = torch.cat(token_hub, dim=1)
        
    #     for b in self.blocks: b.attn.kv_caching(False)
    #     return f_hat, token_hub   # de-normalize, from [-1, 1] to [0, 1]
    


    @torch.no_grad()
    def autoregressive_infer_cfg_draft_beamsearch(
        self, B: int, label_B: Optional[Union[int, torch.LongTensor]],
        g_seed: Optional[int] = None, cfg=1.5, top_k=0, top_p=0.0,
        more_smooth=False, exit_num=7, temp=1, beamwidth=2
    ) -> torch.Tensor:
        if g_seed is None: rng = None
        else: self.rng.manual_seed(g_seed); rng = self.rng
        
        if label_B is None:
            label_B = torch.multinomial(self.uniform_prob, num_samples=B, replacement=True, generator=rng).reshape(B)
        elif isinstance(label_B, int):
            label_B = torch.full((B,), fill_value=self.num_classes if label_B < 0 else label_B, device=self.lvl_1L.device)

        # Initial setup remains the same
        sos = cond_BD = self.class_emb(torch.cat((label_B, torch.full_like(label_B, fill_value=self.num_classes)), dim=0))
        lvl_pos = self.lvl_embed(self.lvl_1L) + self.pos_1LC
        next_token_map = sos.unsqueeze(1).expand(2 * B, self.first_l, -1) + self.pos_start.expand(2 * B, self.first_l, -1) + lvl_pos[:, :self.first_l]
        
        cur_L = 0
        f_hat = sos.new_zeros(B, self.Cvae, self.patch_nums[-1], self.patch_nums[-1])
        
        cond_BD_or_gss = self.shared_ada_lin(cond_BD)
        cond_BD_base = cond_BD.clone()
        cond_BD_or_gss_base = cond_BD_or_gss.clone()
        
        # Initialize token storage for each beam
        token_storage = {i: [] for i in range(B*beamwidth**2)}
        best_beam_indices = torch.zeros(B, dtype=torch.long)
        
        # Enable KV caching for all blocks
        for b in self.blocks:
            b.attn.kv_caching(True)
        
        active_beams = 1
        
        try:
            for si, pn in enumerate(self.patch_nums):
                if si == exit_num:
                    break
                    
                ratio = si / self.num_stages_minus_1
                cur_L += pn*pn
                x = next_token_map
                
                # Forward pass through transformer blocks
                print("@@@@@@@@@@@@@",si,x.shape,cond_BD_or_gss.shape)
                for b in self.blocks:
                    x = b(x=x, cond_BD=cond_BD_or_gss, attn_bias=None)
                
                logits_BlV = self.get_logits(x, cond_BD)
                t = cfg * ratio
                logits_BlV = (1+t) * logits_BlV[:B] - t * logits_BlV[B:]
                
                if si < 3:  # Early stages: expand beams
                    idx_Bl = sample_with_top_k_top_p_(
                        logits_BlV, 
                        rng=rng, 
                        top_k=top_k[si] if isinstance(top_k, (list, tuple)) else top_k,
                        top_p=top_p, 
                        num_samples=beamwidth, 
                        temp=temp
                    ).view(B*beamwidth, -1)
                    
                    # Store current token maps for each beam
                    current_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                    for batch_idx in range(B):
                        beam_tokens = current_token_map[batch_idx:batch_idx+1]
                        token_storage[batch_idx].append(beam_tokens)
                    
                    # Expand beam dimension for all relevant tensors
                    f_hat = f_hat.repeat(beamwidth, 1, 1, 1)
                    cond_BD_or_gss = cond_BD_or_gss.repeat_interleave(repeats=beamwidth, dim=0)
                    cond_BD = cond_BD.repeat_interleave(repeats=beamwidth, dim=0)
                    
                    B *= beamwidth
                    active_beams = beamwidth
                    
                    # Update KV cache for new beams
                    for b in self.blocks:
                        b.attn.update_cache_for_beams(active_beams)
                    
                elif si == 3:  # Transition stage: reduce beams
                    # Score beams and select the best ones
                    scores = logits_BlV.max(dim=-1)[0].sum(dim=1)
                    scores = scores.view(-1, beamwidth)
                    best_beam_indices = scores.max(dim=1)[1]
                    
                    # Select best beams
                    original_B = B // active_beams
                    base_indices = torch.arange(0, original_B, device=best_beam_indices.device) * beamwidth
                    selected_indices = base_indices + best_beam_indices
                    
                    # Update tensors for selected beams
                    f_hat = f_hat[selected_indices]
                    cond_BD_or_gss = cond_BD_or_gss[selected_indices]
                    cond_BD = cond_BD[selected_indices]
                    
                    # Sample next tokens for selected beams
                    idx_Bl = sample_with_top_k_top_p_(
                        logits_BlV[selected_indices], 
                        rng=rng, 
                        top_k=top_k[si] if isinstance(top_k, (list, tuple)) else top_k,
                        top_p=top_p, 
                        num_samples=1, 
                        temp=temp
                    )[:, :, 0]
                    
                    # Update token storage with selected beam paths
                    current_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                    for batch_idx in range(original_B):
                        selected_beam = best_beam_indices[batch_idx].item()
                        token_storage[batch_idx].append(
                            current_token_map[batch_idx * beamwidth + selected_beam:batch_idx * beamwidth + selected_beam + 1]
                        )
                    
                    B = original_B
                    active_beams = 1
                    
                    # Important: Update KV cache to keep only the selected beam paths
                    for b in self.blocks:
                        # Assuming we have methods to manipulate the cache directly
                        if hasattr(b.attn, 'k_cache'):
                            # Reshape cache to separate beams
                            k_cache = b.attn.k_cache.view(original_B, beamwidth, *b.attn.k_cache.shape[1:])
                            v_cache = b.attn.v_cache.view(original_B, beamwidth, *b.attn.v_cache.shape[1:])
                            
                            # Select the best beam for each batch item
                            for batch_idx in range(original_B):
                                selected_beam = best_beam_indices[batch_idx].item()
                                k_cache[batch_idx] = k_cache[batch_idx, selected_beam]
                                v_cache[batch_idx] = v_cache[batch_idx, selected_beam]
                            
                            # Update the cache with selected beams
                            b.attn.k_cache = k_cache.view(original_B, *b.attn.k_cache.shape[1:])
                            b.attn.v_cache = v_cache.view(original_B, *b.attn.v_cache.shape[1:])
                    
                    # Double the cache for CFG
                    for b in self.blocks:
                        if hasattr(b.attn, 'k_cache'):
                            b.attn.k_cache = b.attn.k_cache.repeat(2, 1, 1)
                            b.attn.v_cache = b.attn.v_cache.repeat(2, 1, 1)
                    
                    # cond_BD_or_gss = cond_BD_or_gss_base
                    # cond_BD = cond_BD_base
                    
                else:  # Later stages: single beam
                    idx_Bl = sample_with_top_k_top_p_(
                        logits_BlV, 
                        rng=rng, 
                        top_k=top_k[si] if isinstance(top_k, (list, tuple)) else top_k,
                        top_p=top_p, 
                        num_samples=1, 
                        temp=temp
                    )[:, :, 0]
                    
                    current_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                    for batch_idx in range(B):
                        token_storage[batch_idx].append(current_token_map[batch_idx:batch_idx+1])
                    
                
                # Generate embeddings and prepare next input (same as before)
                if not more_smooth:
                    h_BChw = self.vae_quant_proxy[0].embedding(idx_Bl)
                else:
                    gum_t = max(0.27 * (1 - ratio * 0.95), 0.005)
                    h_BChw = gumbel_softmax_with_rng(
                        logits_BlV.mul(1 + ratio), 
                        tau=gum_t, 
                        hard=False, 
                        dim=-1, 
                        rng=rng
                    ) @ self.vae_quant_proxy[0].embedding.weight.unsqueeze(0)
                
                h_BChw = h_BChw.transpose_(1, 2).reshape(B, self.Cvae, pn, pn)
                
                f_hat, next_token_map = self.vae_quant_proxy[0].get_next_autoregressive_input(
                    si, len(self.patch_nums), f_hat, h_BChw
                )
                
                next_token_map = next_token_map.view(B, self.Cvae, -1).transpose(1, 2)
                next_token_map = self.word_embed(next_token_map) + lvl_pos[:, cur_L:cur_L + self.patch_nums[si+1] ** 2]
                next_token_map = next_token_map.repeat(2, 1, 1)
                
        finally:
            # Disable KV caching
            for b in self.blocks:
                b.attn.kv_caching(False)
                b.attn.reset_cache()
        
        # Combine tokens from the selected beam paths
        final_tokens = []
        for batch_idx in range(B):
            batch_tokens = torch.cat(token_storage[batch_idx], dim=1)
            final_tokens.append(batch_tokens)
        token_hub = torch.cat(final_tokens, dim=0)
        
        return f_hat, token_hub

    




class VARHF(VAR, PyTorchModelHubMixin):
            # repo_url="https://github.com/FoundationVision/VAR",
            # tags=["image-generation"]):
    def __init__(
        self,
        vae_kwargs,
        num_classes=1000, depth=16, embed_dim=1024, num_heads=16, mlp_ratio=4., drop_rate=0., attn_drop_rate=0., drop_path_rate=0.,
        norm_eps=1e-6, shared_aln=False, cond_drop_rate=0.1,
        attn_l2_norm=False,
        patch_nums=(1, 2, 3, 4, 5, 6, 8, 10, 13, 16),   # 10 steps by default
        flash_if_available=True, fused_if_available=True,
    ):
        vae_local = VQVAE(**vae_kwargs)
        super().__init__(
            vae_local=vae_local,
            num_classes=num_classes, depth=depth, embed_dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, drop_rate=drop_rate, attn_drop_rate=attn_drop_rate, drop_path_rate=drop_path_rate,
            norm_eps=norm_eps, shared_aln=shared_aln, cond_drop_rate=cond_drop_rate,
            attn_l2_norm=attn_l2_norm,
            patch_nums=patch_nums,
            flash_if_available=flash_if_available, fused_if_available=fused_if_available,
        )
