import argparse
import functools
from time import time
from typing import Any

import jax
import jax.numpy as jnp
import numpy as np
from flax.training.train_state import TrainState
from jax.experimental import PartitionSpec
from jax.experimental.maps import Mesh
from jax.experimental.pjit import pjit, with_sharding_constraint
from tqdm import tqdm

from src.models.GPT import model_getter
from src.training.training_utils import TrainState, get_optimizer
from src.utils.partitioning import create_opt_spec, set_partitions_zero

"""
Experimental support for a ZeRO style optimizer partition:
    - Optimizer States are partitioned across devices

"""


def parse():
    parser = argparse.ArgumentParser(description="Pjit benchmarking code")

    parser.add_argument("--grad-accum", default=32, type=int)

    parser.add_argument("--batch-size", default=512, type=int)

    parser.add_argument("--ctx", default=512, type=int)

    args = parser.parse_args()
    return args


if __name__ == "__main__":

    args = parse()

    # CONSTANTS
    GRAD_ACCUM_STEPS = args.grad_accum
    BATCH_SIZE = args.batch_size
    CTX_LEN = args.ctx
    MODEL_SIZE = "base"
    NUM_PASSES = 10

    # Setting up device mesh (dp, mp axes)
    mesh = Mesh(np.asarray(jax.devices(), dtype=object).reshape(jax.local_device_count(),), ['dp']) 

    # Setting up model + param spec
    model = model_getter(MODEL_SIZE, return_cfg=False)
    rng = jax.random.PRNGKey(23)
    batch_tok = jax.random.randint(rng, shape=(1, CTX_LEN), maxval=50257, minval=0)
    param_shape = jax.eval_shape(model.init, rng, batch_tok)
    # Setting up optimizer + opt spec
    tx = get_optimizer(3e-4, 0.01, model, param_shape)

    def init_state(params):

        return TrainState.create(
            apply_fn=model.apply,
            tx=tx,
            params=params,
        )

    def train_step(
        state: Any,
        batch: jnp.array,
        rng_key: jax.random.PRNGKey = None,
        grad_accum_steps: int = None,
    ):
        """Train on a single Gradient-Accumulation batch
        This means that the batch will be size (local_bs*grad_accum, ctx) instead of (local_bs, ctx)

        """

        def get_minibatch(batch, grad_idx):
            return jax.tree_util.tree_map(
                lambda x: jax.lax.dynamic_index_in_dim(
                    x, grad_idx, keepdims=False, axis=1
                ),
                batch,
            )

        def loss_fn(params, batch):
            _, loss = state.apply_fn(
                {"params": params["params"]},
                x=batch,
                labels=batch,
                train=True,
                rngs={"dropout": rng_key},
            )
            return loss

        grad_fn = jax.value_and_grad(loss_fn, has_aux=False)

        def loss_and_grad(grad_idx):
            minibatch = (
                get_minibatch(batch, grad_idx) if grad_idx is not None else batch
            )
            minibatch = with_sharding_constraint(minibatch, PartitionSpec(None,"dp"))

            loss, grads = grad_fn(state.params, minibatch)

            return loss, grads

        # tuple of loss, grads
        init_minibatch = (
            0.0,
            jax.tree_util.tree_map(jnp.zeros_like, state.params)
        )

        # accumulate gradients
        def cumul_minibatch_step(grad_idx, cumul_loss_grad):
            cumul_loss, cumul_grads = cumul_loss_grad
            loss, grads = loss_and_grad(grad_idx)
            cumul_loss, cumul_grads = jax.tree_util.tree_map(
                jnp.add, (cumul_loss, cumul_grads), (loss, grads)
            )
            return cumul_loss, cumul_grads

        loss, grads = jax.lax.fori_loop(
            0,
            grad_accum_steps,
            cumul_minibatch_step,
            init_minibatch,
        )



        # sum -> mean
        loss, grads = jax.tree_util.tree_map(
            lambda x: x / grad_accum_steps, (loss, grads)
        )

        # only update train_state at the end of a single full batch
        new_state = state.apply_gradients(
            grads=grads,
        )

        metrics = {
            "Train LM Loss": loss,
            "Train LM PPL": jnp.exp(loss),
        }

        return new_state, metrics

    with mesh:
        train_step_pjit = pjit(
            functools.partial(
                train_step, grad_accum_steps=GRAD_ACCUM_STEPS
            ),
            in_axis_resources=(None, PartitionSpec(None,"dp"), None),
            out_axis_resources=(None, None),
        )

        rng, dropout_rng = jax.random.split(rng, 2)
        init_batch = jax.numpy.ones(shape=(1, CTX_LEN), dtype=jax.numpy.int32)

        params = model.init(rng, init_batch, train = False)

        state = init_state(params)

        print("State Sharded Sucessfully")

        init_batch = jax.numpy.ones(shape=(BATCH_SIZE, CTX_LEN), dtype=jax.numpy.int32)
        batch = jax.tree_util.tree_map(
            lambda x: x.reshape(
                (GRAD_ACCUM_STEPS,) + (x.shape[0] // GRAD_ACCUM_STEPS,) + x.shape[1:]
            ),
            init_batch,
        ).transpose(1, 0, 2)

        # compile first
        state, metrics = train_step_pjit(state, batch, dropout_rng)

        times = []
        for _ in tqdm(range(NUM_PASSES)):
            rng, batch_rng = jax.random.split(rng, 2)

            # Create a test batch of data
            test_batch = jax.random.randint(
                key=rng,
                shape=(BATCH_SIZE, CTX_LEN),
                dtype=jax.numpy.int32,
                maxval=50257,
                minval=0,
            )
            test_batch = jax.tree_util.tree_map(
                lambda x: x.reshape(
                    (GRAD_ACCUM_STEPS,)
                    + (x.shape[0] // GRAD_ACCUM_STEPS,)
                    + x.shape[1:]
                ),
                test_batch,
            ).transpose(1, 0, 2)

            t0 = time()
            state, metrics = train_step_pjit(state, test_batch, dropout_rng)
            times.append(time() - t0)

    print(
        f"ZeRO Step - Global BS {BATCH_SIZE} - accum steps {GRAD_ACCUM_STEPS} - Num Executions {NUM_PASSES}"
    )
    print(f"Mesh Layout (dp): (8)")
    print(f"Model Size: {MODEL_SIZE}")
    print(f"Mean Batch Time {np.mean(times):.4f} Seconds")
    print()
