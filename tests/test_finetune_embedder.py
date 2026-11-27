"""ml/finetune_embedder.py: поправки LoRA вливаются в веса без изменения векторов; потеря различает товары."""
import pytest

torch = pytest.importorskip("torch")

from ml.finetune_embedder import LoRALinear, add_lora, lora_state, merge_lora, supcon  # noqa: E402


class Tiny(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = torch.nn.Linear(8, 16)
        self.fc2 = torch.nn.Linear(16, 4)
        self.attn = torch.nn.MultiheadAttention(16, 2)      # её out_proj не оборачивается

    def forward(self, x):
        return self.fc2(torch.relu(self.fc1(x)))


def test_lora_starts_as_identity_and_merges_exactly():
    torch.manual_seed(0)
    plain, tuned = Tiny(), Tiny()
    tuned.load_state_dict(plain.state_dict())
    x = torch.randn(5, 8)
    names = add_lora(tuned, rank=2, alpha=2)
    assert sorted(names) == ["fc1", "fc2"]
    assert not isinstance(tuned.attn.out_proj, LoRALinear)
    assert torch.allclose(tuned(x), plain(x))                  # B = 0: модель как была
    with torch.no_grad():
        for m in tuned.modules():
            if isinstance(m, LoRALinear):
                m.lora_b.normal_()
    state = lora_state(tuned)
    assert merge_lora(plain, state) == 2
    assert torch.allclose(tuned(x), plain(x), atol=1e-2)       # поправки хранятся в fp16


def test_supcon_prefers_grouped_vectors():
    labels = torch.tensor([0, 0, 1, 1])
    good = torch.nn.functional.normalize(torch.tensor([[1., 0], [1, .1], [0, 1], [.1, 1]]), dim=1)
    bad = good[[0, 2, 1, 3]]
    assert supcon(good, labels, 0.1) < supcon(bad, labels, 0.1)
