import torch
import torch.nn as nn
from functools import partial
from vision_transformer import VisionTransformer, _cfg
from timm.models.registry import register_model
from timm.models.layers import trunc_normal_
import torch.nn.functional as F

import math


__all__ = [
    'deit_small_MCTformerV1_patch16_224', 'deit_small_MCTformerV2_patch16_224'
]

class MCTformerV2(VisionTransformer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.head = nn.Conv2d(self.embed_dim, self.num_classes, kernel_size=3, stride=1, padding=1)
        self.avgpool = nn.AdaptiveAvgPool2d(1)
        self.head.apply(self._init_weights)
        num_patches = self.patch_embed.num_patches
        self.cls_token = nn.Parameter(torch.zeros(1, self.num_classes, self.embed_dim))
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + self.num_classes, self.embed_dim))

        trunc_normal_(self.cls_token, std=.02)
        trunc_normal_(self.pos_embed, std=.02)
        print(self.training)

    def interpolate_pos_encoding(self, x, w, h):
        npatch = x.shape[1] - self.num_classes
        N = self.pos_embed.shape[1] - self.num_classes
        if npatch == N and w == h:
            return self.pos_embed
        class_pos_embed = self.pos_embed[:, 0:self.num_classes]
        patch_pos_embed = self.pos_embed[:, self.num_classes:]
        dim = x.shape[-1]

        w0 = w // self.patch_embed.patch_size[0]
        h0 = h // self.patch_embed.patch_size[0]
        # we add a small number to avoid floating point error in the interpolation
        # see discussion at https://github.com/facebookresearch/dino/issues/8
        w0, h0 = w0 + 0.1, h0 + 0.1
        patch_pos_embed = nn.functional.interpolate(
            patch_pos_embed.reshape(1, int(math.sqrt(N)), int(math.sqrt(N)), dim).permute(0, 3, 1, 2),
            scale_factor=(w0 / math.sqrt(N), h0 / math.sqrt(N)),
            mode='bicubic',
        )
        assert int(w0) == patch_pos_embed.shape[-2] and int(h0) == patch_pos_embed.shape[-1]
        patch_pos_embed = patch_pos_embed.permute(0, 2, 3, 1).view(1, -1, dim)
        return torch.cat((class_pos_embed, patch_pos_embed), dim=1)

    def forward_features(self, x, n=12):
        B, nc, w, h = x.shape # B 3 224 224
        x = self.patch_embed(x) # B 196 384

        cls_tokens = self.cls_token.expand(B, -1, -1) # B 20 384
        x = torch.cat((cls_tokens, x), dim=1) # B (20+196) 384
        x = x + self.interpolate_pos_encoding(x, w, h) # B (20+196) 384
        x = self.pos_drop(x) # B (20+196) 384
        attn_weights = []

        for i, blk in enumerate(self.blocks):
            x, weights_i = blk(x) # x: B (20+196) 384  weights_i: B 6 (20+196) (20+196)
            attn_weights.append(weights_i) 

        return x[:, 0:self.num_classes], x[:, self.num_classes:], attn_weights

    def forward(self, x, return_att=False, n_layers=12, attention_type='fused'):
        w, h = x.shape[2:] # 224 224
        x_cls, x_patch, attn_weights = self.forward_features(x) #x_cls: B 20 384 x_patch: B 196 384 weights_i: [(B 6 (20+196) (20+196))*12]
        n, p, c = x_patch.shape # x_patch: B 196 384
        if w != h:
            w0 = w // self.patch_embed.patch_size[0]
            h0 = h // self.patch_embed.patch_size[0]
            x_patch = torch.reshape(x_patch, [n, w0, h0, c])
        else:
            x_patch = torch.reshape(x_patch, [n, int(p ** 0.5), int(p ** 0.5), c]) # x_patch: B 14 14 384
        x_patch = x_patch.permute([0, 3, 1, 2]) # x_patch: B 384 14 14
        #x_patch: use to generate cam_patch
        x_patch = x_patch.contiguous()
        x_patch = self.head(x_patch) # x_patch: B 20 14 14
        x_patch_logits = self.avgpool(x_patch).squeeze(3).squeeze(2) # x_patch_logits: B 20

        attn_weights = torch.stack(attn_weights)  # 12 * B * H * N * N   #12(num_of_layer used) B 6(num_of_heads) (20+196) (20+196)
        attn_weights = torch.mean(attn_weights, dim=2)  # 12 * B * N * N   #12(num_of_layer used) B (20+196) (20+196)

        feature_map = x_patch.detach().clone()  # B * C * 14 * 14   feature_map: B 20 14 14
        feature_map = F.relu(feature_map) # feature_map: B 20 14 14
        #feature_map: cam_patch
        n, c, h, w = feature_map.shape #  B 20 14 14

        mtatt = attn_weights[-n_layers:].sum(0)[:, 0:self.num_classes, self.num_classes:].reshape([n, c, h, w]) #  B 20 14 14
        #mtatt: cam_cls

        if attention_type == 'fused':
            cams = mtatt * feature_map  # B * C * 14 * 14 #  B 20 14 14
        elif attention_type == 'patchcam':
            cams = feature_map #  B 20 14 14
        else:
            cams = mtatt #  B 20 14 14

        patch_attn = attn_weights[:, :, self.num_classes:, self.num_classes:]  #12(num_of_layer used) B 196 196
        #patch_attn: affinity matrix

        x_cls_logits = x_cls.mean(-1) #x_cls_logits: B 20 384

        if return_att:
            return x_cls_logits, cams, patch_attn
        #          B 20           B 20 14 14    12 B 196 196
        else:
            return x_cls_logits, x_patch_logits


class MCTformerV1(VisionTransformer):
    def __init__(self, last_opt='average', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_opt = last_opt
        if last_opt == 'fc':
            self.head = nn.Conv1d(in_channels=self.num_classes, out_channels=self.num_classes, kernel_size=self.embed_dim, groups=self.num_classes)
            self.head.apply(self._init_weights)

        num_patches = self.patch_embed.num_patches
        self.cls_token = nn.Parameter(torch.zeros(1, self.num_classes, self.embed_dim))
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + self.num_classes, self.embed_dim))

        trunc_normal_(self.cls_token, std=.02)
        trunc_normal_(self.pos_embed, std=.02)
        print(self.training)

    def interpolate_pos_encoding(self, x, w, h):
        npatch = x.shape[1] - self.num_classes
        N = self.pos_embed.shape[1] - self.num_classes
        if npatch == N and w == h:
            return self.pos_embed
        class_pos_embed = self.pos_embed[:, 0:self.num_classes]
        patch_pos_embed = self.pos_embed[:, self.num_classes:]
        dim = x.shape[-1]

        w0 = w // self.patch_embed.patch_size[0]
        h0 = h // self.patch_embed.patch_size[0]
        # we add a small number to avoid floating point error in the interpolation
        # see discussion at https://github.com/facebookresearch/dino/issues/8
        w0, h0 = w0 + 0.1, h0 + 0.1
        patch_pos_embed = nn.functional.interpolate(
            patch_pos_embed.reshape(1, int(math.sqrt(N)), int(math.sqrt(N)), dim).permute(0, 3, 1, 2),
            scale_factor=(w0 / math.sqrt(N), h0 / math.sqrt(N)),
            mode='bicubic',
        )
        assert int(w0) == patch_pos_embed.shape[-2] and int(h0) == patch_pos_embed.shape[-1]
        patch_pos_embed = patch_pos_embed.permute(0, 2, 3, 1).view(1, -1, dim)
        return torch.cat((class_pos_embed, patch_pos_embed), dim=1)

    def forward_features(self, x, n=12):
        B, nc, w, h = x.shape # B 3 224 224
        x = self.patch_embed(x) # B 196 768

        cls_tokens = self.cls_token.expand(B, -1, -1) # B 20 768
        x = torch.cat((cls_tokens, x), dim=1) # B (20+196) 768
        x = x + self.interpolate_pos_encoding(x, w, h) # B (20+196) 768
        x = self.pos_drop(x) # B (20+196) 768

        attn_weights = []

        for i, blk in enumerate(self.blocks):
            x, weights_i = blk(x) # x: B (20+196) 768 weights_i: B 12 (20+196) (20+196)
            if len(self.blocks) - i <= n:
                attn_weights.append(weights_i)
        return x[:, 0:self.num_classes], attn_weights

    def forward(self, x, n_layers=12, return_att=False):
        x, attn_weights = self.forward_features(x) #x: B 20 768   weights_i: [(B 12 (20+196) (20+196))*12]

        attn_weights = torch.stack(attn_weights)  # 12 * B * H * N * N                12(num_of_layer used) B 12(num_of_heads) (20+196) (20+196)
        attn_weights = torch.mean(attn_weights, dim=2)  # 12 * B * N * N   12(num_of_layer used) B (20+196) (20+196)
        mtatt = attn_weights[-n_layers:].sum(0)[:, 0:self.num_classes, self.num_classes:]  #B 20 196
        patch_attn = attn_weights[:, :, self.num_classes:, self.num_classes:] # 12(num_of_layer used) B 196 196

        x_cls_logits = x.mean(-1) # B 20 

        if return_att:
            return x_cls_logits, mtatt, patch_attn 
        #          B 20           B 20 196    12 B 196 196
        else:
            return x_cls_logits


@register_model
def deit_small_MCTformerV2_patch16_224(pretrained=False, **kwargs):
    model = MCTformerV2(
        patch_size=16, embed_dim=384, depth=12, num_heads=6, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        checkpoint = torch.hub.load_state_dict_from_url(
            url="https://dl.fbaipublicfiles.com/deit/deit_small_patch16_224-cd65a155.pth",
            map_location="cpu", check_hash=True
        )['model']
        model_dict = model.state_dict()
        for k in ['head.weight', 'head.bias', 'head_dist.weight', 'head_dist.bias']:
            if k in checkpoint and checkpoint[k].shape != model_dict[k].shape:
                print(f"Removing key {k} from pretrained checkpoint")
                del checkpoint[k]
        pretrained_dict = {k: v for k, v in checkpoint.items() if k in model_dict}
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k not in ['cls_token', 'pos_embed']}
        model_dict.update(pretrained_dict)
        model.load_state_dict(model_dict)
    return model


@register_model
def deit_small_MCTformerV1_patch16_224(pretrained=False, **kwargs):
    model = MCTformerV1(
        patch_size=16, embed_dim=384, depth=12, num_heads=6, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        checkpoint = torch.hub.load_state_dict_from_url(
            url="https://dl.fbaipublicfiles.com/deit/deit_small_patch16_224-cd65a155.pth",
            map_location="cpu", check_hash=True
        )
        model.load_state_dict(checkpoint["model"])

    return model
