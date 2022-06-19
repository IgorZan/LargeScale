# Extracted from: https://github.com/EleutherAI/gpt-neox
import torch
import torch.nn.functional as F


class RotaryEmbedding(torch.nn.Module):

    def __init__(self, dim, base=10000, precision=torch.half, learnable=False):
        super().__init__()
        inv_freq = 1. / (base ** (torch.arange(0, dim, 2).float() / dim))
        self.learnable = learnable
        if learnable:
            self.inv_freq = torch.nn.Parameter(inv_freq)
            self.max_seq_len_cached = None
        else:
            self.register_buffer('inv_freq', inv_freq)
            self.max_seq_len_cached = None
            self.cos_cached = None
            self.sin_cached = None
        self.precision = precision

    def forward(self, x, seq_dim=1, seq_len=None):
        if seq_len is None:
            seq_len = x.shape[seq_dim]
        if self.max_seq_len_cached is None or (seq_len > self.max_seq_len_cached):
            self.max_seq_len_cached = None if self.learnable else seq_len
            t = torch.arange(seq_len, device=x.device, dtype=self.inv_freq.dtype)
            freqs = torch.einsum('i,j->ij', t, self.inv_freq)
            # Different from paper, but it uses a different permutation in order to obtain the same calculation
            emb = torch.cat((freqs, freqs), dim=-1).to(x.device)
            if self.precision == torch.bfloat16:
                emb = emb.float()
            # [sx, 1 (b * np), hn]
            cos_cached = emb.cos()[:, None, :]
            sin_cached = emb.sin()[:, None, :]
            if self.precision == torch.bfloat16:
                cos_cached = cos_cached.bfloat16()
                sin_cached = sin_cached.bfloat16()
            if self.learnable:
                return cos_cached, sin_cached
            self.cos_cached, self.sin_cached = cos_cached, sin_cached
        return self.cos_cached[:seq_len, ...], self.sin_cached[:seq_len, ...]


class RotaryPositionalEmbeddingFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, q, cos, sin):
        import rotary_positional_embedding_cuda

        q_ = q.contiguous()
        cos_ = cos.contiguous()
        sin_ = sin.contiguous()
        output = rotary_positional_embedding_cuda.forward(*q.shape, q_, cos_, sin_)
        ctx.save_for_backward(cos_, sin_)

        return output

    @staticmethod
    def backward(ctx, grad_output):
        import rotary_positional_embedding_cuda

        cos_, sin_ = ctx.saved_tensors
        grad_q = rotary_positional_embedding_cuda.backward(*grad_output.shape, grad_output, cos_, sin_)

        return grad_q, None, None

# rotary pos emb helpers:

def rotate_half(x):
    x1, x2 = x[..., :x.shape[-1] // 2], x[..., x.shape[-1] // 2:]
    return torch.cat((-x2, x1), dim=x1.ndim - 1)  # dim=-1 triggers a bug in earlier torch versions


@torch.jit.script
def apply_rotary_pos_emb(q, k, cos, sin, offset: int = 0):
    cos, sin = cos[offset:q.shape[0] + offset, ...], sin[offset:q.shape[0] + offset, ...]
    return (q * cos) + (rotate_half(q) * sin), (k * cos) + (rotate_half(k) * sin)


def apply_rotary_pos_emb_torch(q, k, cos, sin, offset: int = 0):  # jitting fails with bf16
    cos, sin = cos[offset:q.shape[0] + offset, ...], sin[offset:q.shape[0] + offset, ...]
    return (q * cos) + (rotate_half(q) * sin), (k * cos) + (rotate_half(k) * sin)


def apply_rotary_pos_emb_fused(q, k, cos, sin, offset: int = 0):
    cos, sin = cos[offset:q.shape[0] + offset, ...], sin[offset:q.shape[0] + offset, ...]
    q = RotaryPositionalEmbeddingFunction.apply(q, cos, sin)
    k = RotaryPositionalEmbeddingFunction.apply(k, cos, sin)
    return q, k


@torch.jit.script
def apply_rotary_pos_emb_index(h, cos, sin, position_id):
    # position_id: [sq, b], q: [sq, b * np, hn] -> [sq, b, np, hn], cos: [sq, 1, hn] -> [sq, b, 1, hn]
    sq, b, np = position_id.size(0), position_id.size(1), h.size(1) // position_id.size(1)
    h = h.view(sq, b, np, -1)
    cos, sin = F.embedding(position_id, cos.squeeze(1)).unsqueeze(2), \
               F.embedding(position_id, sin.squeeze(1)).unsqueeze(2)
    h = (h * cos) + (rotate_half(h) * sin)
    return h.view(sq, b * np, -1)


def apply_rotary_pos_emb_index_torch(h, cos, sin, position_id):  # jitting fails with bf16
    sq, b, np = position_id.size(0), position_id.size(1), h.size(1) // position_id.size(1)
    h = h.view(sq, b, np, -1)
    cos, sin = F.embedding(position_id, cos.squeeze(1)).unsqueeze(2), \
               F.embedding(position_id, sin.squeeze(1)).unsqueeze(2)
    h = (h * cos) + (rotate_half(h) * sin)
    return h.view(sq, b * np, -1)


def apply_rotary_pos_emb_index_fused(h, cos, sin, position_id):
    # position_id: [sq, b], h: [sq, b * np, hn] -> [sq, b, np, hn], cos: [sq, 1, hn] -> [sq, b, 1, hn]
    sq, b, np = position_id.size(0), position_id.size(1), h.size(1) // position_id.size(1)
    h = h.view(sq, b, np, -1)
    cos, sin = F.embedding(position_id, cos.squeeze(1)).unsqueeze(2), \
               F.embedding(position_id, sin.squeeze(1)).unsqueeze(2)
    h = RotaryPositionalEmbeddingFunction.apply(h, cos, sin)
    return h.view(sq, b * np, -1)
