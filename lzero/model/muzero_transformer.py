import torch
import torch.nn as nn
import torch.nn.functional as F
import selfies as sf
from tokenizers import Tokenizer, processors, models
from tokenizers.implementations import BaseTokenizer
from typing import List, Tuple, Optional
from ding.utils import MODEL_REGISTRY, SequenceType
from .common import MZNetworkOutput

# -----------------------------------------------------------------------------
# Special-token constants
# -----------------------------------------------------------------------------
PAD_TOKEN = "<pad>"
SOS_TOKEN = "<s>"
EOS_TOKEN = "</s>"
UNK_TOKEN = "<unk>"

class SpecialTokensBaseTokenizer(BaseTokenizer):
    def __init__(self, tokenizer: Tokenizer, max_len: int):
        super().__init__(tokenizer)
        self.pad_token = PAD_TOKEN
        self.sos_token = SOS_TOKEN
        self.eos_token = EOS_TOKEN
        self.unk_token = UNK_TOKEN
        self.max_length = max_len
        # Ensure REMOVE and END are in vocab
        self._tokenizer.add_tokens(["<REMOVE>", "<END>"])
        # Add special tokens
        self.add_special_tokens([self.pad_token, self.sos_token, self.eos_token, self.unk_token])
        # Record token IDs
        self.pad_token_id = self.token_to_id(self.pad_token)
        self.sos_token_id = self.token_to_id(self.sos_token)
        self.eos_token_id = self.token_to_id(self.eos_token)
        self.unk_token_id = self.token_to_id(self.unk_token)
        self.remove_token_id = self.token_to_id("<REMOVE>")
        self.end_token_id    = self.token_to_id("<END>")
        # Enable padding and truncation
        self.enable_padding(direction="right", pad_token=self.pad_token, pad_id=self.pad_token_id, length=max_len)
        self.enable_truncation(max_len)
        # Post processor for adding SOS/EOS
        self._tokenizer.post_processor = processors.TemplateProcessing(
            single=f"{self.sos_token} $A {self.eos_token}",
            pair=f"{self.sos_token} $A {self.eos_token} {self.sos_token} $B {self.eos_token}",
            special_tokens=[(self.sos_token, self.sos_token_id), (self.eos_token, self.eos_token_id)],
        )

class SelfiesTokenizer(SpecialTokensBaseTokenizer):
    def __init__(self, max_len: int):
        alphabet = list(sorted(sf.get_semantic_robust_alphabet()))
        vocab = {symbol: i for i, symbol in enumerate(alphabet)}
        vocab[UNK_TOKEN] = len(vocab)
        tokenizer = Tokenizer(models.WordLevel(vocab=vocab, unk_token=UNK_TOKEN))
        super().__init__(tokenizer, max_len)

    def encode_selfies(self, selfies_str: str, add_special_tokens: bool = True) -> List[int]:
        tokens = list(sf.split_selfies(selfies_str))
        return super().encode(tokens, is_pretokenized=True, add_special_tokens=add_special_tokens).ids

    def decode_to_selfies(self, token_ids: List[int], skip_special_tokens: bool = True) -> str:
        text = super().decode(token_ids, skip_special_tokens=skip_special_tokens)
        return text.replace(" ", "")

# -----------------------------------------------------------------------------
# Utility: pad sequence to max_len
# -----------------------------------------------------------------------------
def pad_to_maxlen(ids: List[int], max_len: int, pad_id: int) -> Tuple[torch.Tensor, torch.Tensor]:
    if len(ids) > max_len:
        raise ValueError(f"Sequence too long: {len(ids)} > {max_len}")
    padded = ids + [pad_id] * (max_len - len(ids))
    mask   = [1] * len(ids) + [0] * (max_len - len(ids))
    return torch.tensor(padded, dtype=torch.long), torch.tensor(mask, dtype=torch.bool)

# -----------------------------------------------------------------------------
# Encoder-Decoder Transformer
# -----------------------------------------------------------------------------
class MassSelfiesED(nn.Module):
    def __init__(self, vocab_size: int, d_model=512, n_enc=4, n_dec=6, n_head=8, dropout=0.1, device="cuda"):
        super().__init__()
        self.device = torch.device(device)
        # Encoder for spectrum vector
        self.spec_proj = nn.Sequential(nn.Linear(4096, d_model), nn.ReLU(), nn.Dropout(dropout))
        enc_layer = nn.TransformerEncoderLayer(d_model=d_model, nhead=n_head, dropout=dropout, batch_first=True)
        self.encoder = nn.TransformerEncoder(enc_layer, num_layers=n_enc)
        # Decoder for token sequence
        self.token_embed = nn.Embedding(vocab_size, d_model)
        self.pos_embed   = nn.Embedding(1024, d_model)
        dec_layer = nn.TransformerDecoderLayer(d_model=d_model, nhead=n_head, dropout=dropout, batch_first=True)
        self.decoder   = nn.TransformerDecoder(dec_layer, num_layers=n_dec)
        # Output heads
        self.action_head = nn.Linear(d_model, vocab_size, bias=False)
        self.value_head  = nn.Sequential(nn.Linear(d_model, d_model), nn.Tanh(), nn.Linear(d_model, 1))
        self.to(self.device)

    def _generate_square_subsequent_mask(self, sz: int) -> torch.Tensor:
        return torch.triu(torch.full((sz, sz), float('-inf')), diagonal=1)

    def forward(self, spectrum_embed: torch.Tensor, tgt_tokens: torch.Tensor, tgt_mask: torch.Tensor):
        # spectrum_embed: (B,4096), tgt_tokens: (B,T), tgt_mask: (B,T)
        spectrum_embed = spectrum_embed.to(self.device)
        tgt_tokens = tgt_tokens.to(self.device)
        tgt_mask = tgt_mask.to(self.device)


        B, T = tgt_tokens.shape
        # Encoder
        enc_feat = self.spec_proj(spectrum_embed)        # (B,d_model)
        mem      = self.encoder(enc_feat.unsqueeze(1))   # (B,1,d_model)
        # Decoder
        pos_ids  = torch.arange(T, device=tgt_tokens.device).unsqueeze(0)
        dec_in   = self.token_embed(tgt_tokens) + self.pos_embed(pos_ids)
        key_pad  = tgt_mask == 0
        causal   = self._generate_square_subsequent_mask(T).to(tgt_tokens.device)
        dec_out  = self.decoder(tgt=dec_in, memory=mem, tgt_mask=causal,
                                tgt_key_padding_mask=key_pad)
        last     = dec_out[:, -1, :]  # (B,d_model)
        logits   = self.action_head(last)  # (B,vocab_size)
        value    = self.value_head(last).squeeze(-1)  # (B)
        return logits, value

# -----------------------------------------------------------------------------
# Greedy step prediction helper
# -----------------------------------------------------------------------------
@torch.no_grad()
def step_prediction(model: MassSelfiesED, tokenizer: SelfiesTokenizer,
                    spectrum_vec: torch.Tensor, prefix_selfies: Optional[torch.Tensor]=None,
                    device: Optional[str]=None):
    model.eval()
    # determine device
    dev = device if device is not None else next(model.parameters()).device
    spectrum_vec = spectrum_vec.to(dev)
    spec = spectrum_vec
    # ensure batch dim
    if spec.dim() == 1:
        spec = spec.unsqueeze(0)
    spec = spec.to(dev)
    # process prefix IDs
    if prefix_selfies is not None:
        ps = prefix_selfies
        if ps.dim() == 2:
            ps = ps.squeeze(0)
        current_ids = [tokenizer.sos_token_id] + ps.tolist()
    else:
        current_ids = [tokenizer.sos_token_id]
    # pad and mask
    max_len = tokenizer.max_length or 64
    ids, mask = pad_to_maxlen(current_ids, max_len, tokenizer.pad_token_id)
    ids  = ids.unsqueeze(0).to(dev)
    mask = mask.unsqueeze(0).to(dev)
    # forward
    logits, value = model(spec, ids, mask)
    # greedy select one token
    next_id = torch.argmax(logits[0]).item()
    return {
        'logits': logits.squeeze(0),      # (vocab_size,)
        'value':  value.detach(),          # (B,) tensor
        'probs':  F.softmax(logits, dim=-1).squeeze(0).cpu().numpy(),
        'current_prefix': current_ids,
        'next_token_id': next_id,
    }

# -----------------------------------------------------------------------------
# MuZero transformer wrapper
# -----------------------------------------------------------------------------
@MODEL_REGISTRY.register('MuZeroSelfiesTransformer')
class MuZeroSelfiesTransformer(nn.Module):
    def __init__(self, observation_shape=4096, max_len=128,
                 d_model=512, n_enc=4, n_dec=6, n_head=8,
                 dropout=0.1, device='cuda', **kwargs):
        super().__init__()
        # tokenizer and transformer
        self.tok = SelfiesTokenizer(max_len=max_len)
        vocab_size = len(self.tok.get_vocab())
        self.transformer = MassSelfiesED(vocab_size=vocab_size,
                                         d_model=d_model, n_enc=n_enc,
                                         n_dec=n_dec, n_head=n_head,
                                         dropout=dropout, device=device)
        self.device = torch.device(device)
        self.to(self.device)
        self.cached_spectrum = None

    def initial_inference(self, obs: torch.Tensor, prefix: torch.Tensor):
        # ensure batch dim for obs
        spec = obs if obs.dim()==2 else obs.unsqueeze(0)
        self.cached_spectrum = spec.to(self.device)
        B = spec.size(0)
        # one-step prediction
        pred = step_prediction(self.transformer, self.tok, spec, prefix, device=self.device)

        # value: shape (B,1)
        val = pred['value']
        val = val.unsqueeze(-1) if val.dim()==1 else val
        val = val.expand(B, 1)
        # reward: (B,1)
        rew = torch.zeros_like(val)
        # policy_logits: (B, action_dim)
        pol = pred['logits'].unsqueeze(0).expand(B, -1)
        
        # latent_state: prefix IDs should be (B,H)
        next_token = torch.argmax(pol, dim=1, keepdim=True)
        lat = prefix if prefix.dim()==2 else prefix.unsqueeze(0)
        new_prefix = torch.cat([lat, next_token], dim=1)
        
        latent = new_prefix.unsqueeze(-1).unsqueeze(-1)

        # return val, rew, pol, latent
        return MZNetworkOutput(value=val, reward=rew, policy_logits=pol, latent_state=lat)

    def _representation(self, observation: torch.Tensor) -> torch.Tensor:
        """
        Overview:
            Simply return the prefix as the latent state representation.
            For molecule generation with SELFIES, the state is just the current sequence of tokens.
        Arguments:
            - observation (:obj:`torch.Tensor`): The current prefix of SELFIES tokens.
        Returns:
            - latent_state (:obj:`torch.Tensor`): The same prefix tensor.
        """
        return observation

    def _dynamics(self, latent_state: torch.Tensor, action: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Overview:
            Concatenate the current prefix with the new token to get the next state.
            For molecule generation with SELFIES, the dynamics is just appending the new token.
        Arguments:
            - latent_state (:obj:`torch.Tensor`): The current prefix of SELFIES tokens.
            - action (:obj:`torch.Tensor`): The next token to append.
        Returns:
            - next_latent_state (:obj:`torch.Tensor`): The concatenated prefix with new token.
            - reward (:obj:`torch.Tensor`): Zero reward tensor since we don't use rewards in this setting.
        """
        # Ensure action has the right shape for concatenation
        action = action.unsqueeze(1) if action.dim() == 1 else action
        
        # Concatenate current prefix with new token
        next_latent_state = torch.cat([latent_state, action], dim=1)
        
        # Create zero reward tensor with same batch size
        reward = torch.zeros(latent_state.size(0), 1, device=latent_state.device)
        
        return next_latent_state, reward

    def recurrent_inference(self, latent_state: torch.Tensor, action: torch.Tensor):
        """
        Overview:
            Perform recurrent inference using the simplified representation and dynamics.
        Arguments:
            - latent_state (:obj:`torch.Tensor`): The current prefix of SELFIES tokens.
            - action (:obj:`torch.Tensor`): The next token to append.
        Returns:
            - value (:obj:`torch.Tensor`): Predicted value for the next state.
            - reward (:obj:`torch.Tensor`): Zero reward tensor.
            - policy_logits (:obj:`torch.Tensor`): Predicted policy logits for the next state.
            - next_latent_state (:obj:`torch.Tensor`): The concatenated prefix with new token.
        """
        # Get next state and reward using dynamics
        next_latent_state, reward = self._dynamics(latent_state, action)
        
        # Pad the new prefix for transformer input
        ids, mask = self._pad_batch_prefix(next_latent_state)
        
        # Get predictions from transformer
        logits, value = self.transformer(self.cached_spectrum, ids, mask)
        
        # Reshape value to match expected format
        value = value.unsqueeze(-1)

        


        # return value, reward, logits, next_latent_state
        return MZNetworkOutput(value=value, reward=reward, policy_logits=logits, latent_state=next_latent_state)

    def _pad_batch_prefix(self, prefix_ids: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        B, L = prefix_ids.shape
        pad_len = self.tok.max_length - L
        if pad_len < 0:
            raise ValueError(f"prefix length {L} exceeds max_len {self.tok.max_length}")
        pad = prefix_ids.new_full((B, pad_len), self.tok.pad_token_id)
        inp = torch.cat([prefix_ids, pad], dim=1)
        msk = torch.cat([torch.ones(B, L, dtype=torch.bool, device=prefix_ids.device),
                         torch.zeros(B, pad_len, dtype=torch.bool, device=prefix_ids.device)], dim=1)
        return inp, msk

if __name__ == "__main__":
    # quick sanity check
    prefix = torch.randint(0, 10, (1, 5)) # torch tensor of shape (1, 5) (B, Length)
    data   = torch.randn(4096)
    model  = MuZeroSelfiesTransformer()
    out = model.initial_inference(data, prefix)
    print([t.shape for t in (out[0], out[1], out[2], out[3])])
    print(out)
