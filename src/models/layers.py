import math
from functools import partial
from typing import Any, List, Tuple

import flax.linen as nn
import jax
import jax.nn.initializers as initializers
import jax.numpy as jnp


def get_slopes(n: int) -> List:
    def get_slopes_power_of_2(n):
        start = 2 ** (-(2 ** -(math.log2(n) - 3)))
        ratio = start
        return [start * ratio**i for i in range(n)]

    if math.log2(n).is_integer():
        return get_slopes_power_of_2(n)
    else:
        closest_power_of_2 = 2 ** math.floor(math.log2(n))
        return (
            get_slopes_power_of_2(closest_power_of_2)
            + get_slopes(2 * closest_power_of_2)[0::2][: n - closest_power_of_2]
        )


class MLPBlock(nn.Module):
    """Standard MLP Block"""

    embedding_dim: int
    dimension_multiplier: int = 4
    dropout: float = 0.0
    N: int = None

    dtype: Any = jnp.float32

    @nn.compact
    def __call__(self, x: jnp.array, train: bool) -> jnp.array:
        dropout = partial(nn.Dropout, rate=self.dropout, deterministic=not train)
        x = nn.Dense(
            features=self.dimension_multiplier * self.embedding_dim,
            name="fc_in",
            kernel_init=initializers.normal(stddev=0.02),
            bias_init=initializers.zeros,
            dtype=self.dtype,
        )(x)
        x = nn.gelu(x)
        out = nn.Dense(
            features=self.embedding_dim,
            name="fc_residual",
            kernel_init=initializers.normal(stddev=(0.02 / jnp.sqrt(2 * self.N))),
            bias_init=initializers.zeros,
            dtype=self.dtype,
        )(x)
        self.sow("intermediates", "mlp_out", x)
        return dropout()(out)


class CausalAttention(nn.Module):
    """Standard causal multi-headed attention

    Supports:
    - ALiBi attention biasing from
    `Train Short, Test Long: Attention with Linear Biases Enables Input
    Length Extrapolation <https://ofir.io/train_short_test_long.pdf>`

    - QKNorm from
    `Query-Key Normalization for Transformers`
    <https://arxiv.org/abs/2010.04245>

    """

    embedding_dim: int
    num_head: int
    block_size: int
    dropout: float = 0.0
    N: int = None
    alibi_attn: bool = False
    dtype: Any = jnp.float32
    qk_norm: bool = False

    def setup(self):
        self.slopes = jnp.array(get_slopes(self.num_head))

        if self.qk_norm:
            self.scale = self.param(
                "attention_scale",
                jax.nn.initializers.ones,
                (self.num_head,),
                jnp.float32,
            )

    @nn.compact
    def __call__(
        self,
        x: jnp.array,
        train: bool,
        alibi_mask: jnp.array = None,
        use_cache: bool = False,
        layer_past: Tuple[jnp.array, jnp.array] = None,
    ) -> Tuple[jnp.array, jnp.array]:
        dropout = partial(nn.Dropout, rate=self.dropout, deterministic=not train)
        B, T, C = x.shape[:3]

        # Shape is (B, nh, T, h_dim)
        key = (
            nn.Dense(
                name="key_proj",
                features=self.embedding_dim,
                kernel_init=initializers.normal(stddev=0.02),
                bias_init=initializers.zeros,
                dtype=self.dtype,
            )(x)
            .reshape(B, T, self.num_head, self.embedding_dim // self.num_head)
            .transpose(0, 2, 1, 3)
        )

        # Shape is (B, nh, T, h_dim)
        value = (
            nn.Dense(
                name="value_proj",
                features=self.embedding_dim,
                kernel_init=initializers.normal(stddev=0.02),
                bias_init=initializers.zeros,
                dtype=self.dtype,
            )(x)
            .reshape(B, T, self.num_head, self.embedding_dim // self.num_head)
            .transpose(0, 2, 1, 3)
        )

        # Shape is (B, nh, T, h_dim)
        query = (
            nn.Dense(
                name="query_proj",
                features=self.embedding_dim,
                kernel_init=initializers.normal(stddev=0.02),
                bias_init=initializers.zeros,
                dtype=self.dtype,
            )(x)
            .reshape(B, T, self.num_head, self.embedding_dim // self.num_head)
            .transpose(0, 2, 1, 3)
        )

        present = None
        if use_cache:
            if layer_past is not None:
                past_keys, past_values = layer_past  # (1, nh, T, h_dim)
                # get shape here, we only keep the past block_size values so lax.scan is happy that we are passing stuff with a fixed size over
                key = jnp.concatenate((past_keys, key), axis=-2)[
                    :, :, -self.block_size :, :
                ]
                value = jnp.concatenate((past_values, value), axis=-2)[
                    :, :, -self.block_size :, :
                ]

            present = jnp.stack((key, value))

        if self.qk_norm:
            query /= jnp.linalg.norm(query, ord=2, axis=-1, keepdims=True)
            key /= jnp.linalg.norm(key, ord=2, axis=-1, keepdims=True)

            scale_factor = self.scale.reshape(1, -1, 1, 1)
            attn_full = scale_factor * (query @ key.transpose(0, 1, 3, 2))

        else:
            # get raw attention scores
            attn_full = (query @ key.transpose(0, 1, 3, 2)) / jnp.sqrt(
                key.shape[-1]
            )  # Shape is (B, nh, sq, sk)

        if self.alibi_attn:

            seq_len_k, seq_len_q = key.shape[-2], query.shape[-2]

            if alibi_mask is None:

                a = -jnp.tril(
                    jnp.tile(
                        jnp.arange(seq_len_k).reshape(seq_len_k, 1), (1, seq_len_k)
                    )
                    + jnp.arange(0, -seq_len_k, step=-1)
                )

                a = a * (self.slopes.reshape(self.slopes.shape[0], 1, 1))

                alibi_mask = a[:, seq_len_k - 1, :].reshape(a.shape[0], 1, a.shape[2])

                attn_full = attn_full + alibi_mask

        mask = jnp.tril(jnp.ones((T, T), dtype=jnp.int8)).reshape(1, 1, T, T)

        masked_attn = jnp.where(mask, attn_full, jnp.finfo(self.dtype).min)

        attn_scores = nn.softmax(masked_attn, axis=-1)
        attn_out = (attn_scores @ value).transpose(
            0, 2, 1, 3
        )  # Shape is (B, T, nh, h_dim)

        attn_out = attn_out.reshape(B, T, C)
        out = nn.Dense(
            name="residual_out",
            features=self.embedding_dim,
            kernel_init=jax.nn.initializers.normal(
                stddev=(0.02 / jnp.sqrt(2 * self.N))
            ),
            bias_init=initializers.zeros,
            dtype=self.dtype,
        )(attn_out)

        self.sow("intermediates", "attn_out", out)

        return dropout()(out), present
