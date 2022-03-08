import tensorflow as tf
import numpy as np
import time
from .sample_mlp import SampleMLP
from .frame_rnn import FrameRNN
from .utils import unsqueeze


class SampleRNN(tf.keras.Model):

    def __init__(self, **kwargs):
        super(SampleRNN, self).__init__()
        self.batch_size = kwargs.get('batch_size')
        self.big_frame_size = kwargs.get('frame_sizes')[1]
        self.frame_size = kwargs.get('frame_sizes')[0]
        self.q_type = kwargs.get('q_type')
        self.q_levels = kwargs.get('q_levels')
        self.dim = kwargs.get('dim')
        self.rnn_type = kwargs.get('rnn_type')
        self.num_rnn_layers = kwargs.get('num_rnn_layers')
        self.seq_len = kwargs.get('seq_len')
        self.emb_size = kwargs.get('emb_size')
        self.skip_conn = kwargs.get('skip_conn')
        self.mixed_precision = kwargs.get('mixed_precision')

        self.big_frame_rnn = FrameRNN(
            rnn_type = self.rnn_type,
            frame_size = self.big_frame_size,
            num_lower_tier_frames = self.big_frame_size // self.frame_size,
            num_layers = self.num_rnn_layers,
            dim = self.dim,
            q_levels = self.q_levels,
            skip_conn = self.skip_conn,
            dropout=kwargs.get('rnn_dropout')
        )

        self.frame_rnn = FrameRNN(
            rnn_type = self.rnn_type,
            frame_size = self.frame_size,
            num_lower_tier_frames = self.frame_size,
            num_layers = self.num_rnn_layers,
            dim = self.dim,
            q_levels = self.q_levels,
            skip_conn = self.skip_conn,
            dropout=kwargs.get('rnn_dropout')
        )

        self.sample_mlp = SampleMLP(
            self.frame_size, self.dim, self.q_levels, self.emb_size
        )

    @tf.function
    def train_step(self, data):
        (x, y) = data
        with tf.GradientTape() as tape:
            raw_output = self(x, training=True)
            prediction = tf.reshape(raw_output, [-1, self.q_levels])
            target = tf.reshape(y, [-1])
            loss = self.compiled_loss(
                target,
                prediction,
                regularization_losses=self.losses)
            if (self.mixed_precision == True):
                loss = self.optimizer.get_scaled_loss(loss)
        grads = tape.gradient(loss, self.trainable_variables)
        if (self.mixed_precision == True):
            grads = self.optimizer.get_unscaled_gradients(grads)
        grads, _ = tf.clip_by_global_norm(grads, 5.0)
        self.optimizer.apply_gradients(zip(grads, self.trainable_variables))
        self.compiled_metrics.update_state(target, prediction)
        return {metric.name: metric.result() for metric in self.metrics}

    @tf.function
    def test_step(self, data):
        (x, y) = data
        raw_output = self(x, training=True)
        prediction = tf.reshape(raw_output, [-1, self.q_levels])
        target = tf.reshape(y, [-1])
        loss = self.compiled_loss(
            target,
            prediction,
            regularization_losses=self.losses)
        self.compiled_metrics.update_state(target, prediction)
        return {metric.name: metric.result() for metric in self.metrics}

    # Sampling function
    def sample(self, samples, temperature):
        samples = tf.nn.log_softmax(samples)
        samples = tf.cast(samples, tf.float64)
        samples = samples / temperature
        sample = tf.random.categorical(samples, 1)
        return tf.cast(sample, tf.int32)

    # Inference
    @tf.function
    def inference_step(self, inputs, temperature):
        num_samps = self.big_frame_size
        samples = inputs
        big_frame_outputs = self.big_frame_rnn(tf.cast(inputs, tf.float32))
        for t in range(num_samps, num_samps * 2):
            if t % self.frame_size == 0:
                frame_inputs = samples[:, t - self.frame_size : t, :]
                big_frame_output_idx = (t // self.frame_size) % (
                    self.big_frame_size // self.frame_size
                )
                frame_outputs = self.frame_rnn(
                    tf.cast(frame_inputs, tf.float32),
                    conditioning_frames=unsqueeze(big_frame_outputs[:, big_frame_output_idx, :], 1))
            sample_inputs = samples[:, t - self.frame_size : t, :]
            frame_output_idx = t % self.frame_size
            sample_outputs = self.sample_mlp(
                sample_inputs,
                conditioning_frames=unsqueeze(frame_outputs[:, frame_output_idx, :], 1))
            # Generate
            sample_outputs = tf.reshape(sample_outputs, [-1, self.q_levels])
            generated = self.sample(sample_outputs, temperature)
            generated = tf.reshape(generated, [self.batch_size, 1, 1])
            samples = tf.concat([samples, generated], axis=1)
        return samples[:, num_samps:]

    def reset_rnn_states(self):
        self.big_frame_rnn.reset_states()
        self.frame_rnn.reset_states()

    def cast(self, inputs):
        # Casts to float16, the policy's lowest-precision dtype
        dtype = self.compute_dtype if self.mixed_precision==True else tf.float32
        return tf.cast(inputs, dtype)

    def call(self, inputs, training=True, temperature=1.0):
        if training==True:
            # UPPER TIER
            big_frame_outputs = self.big_frame_rnn(
                self.cast(inputs)[:, : -self.big_frame_size, :]
            )
            # MIDDLE TIER
            frame_outputs = self.frame_rnn(
                self.cast(inputs)[:, self.big_frame_size-self.frame_size : -self.frame_size, :],
                conditioning_frames=big_frame_outputs,
            )
            # LOWER TIER (SAMPLES)
            sample_output = self.sample_mlp(
                inputs[:, self.big_frame_size - self.frame_size : -1, :],
                conditioning_frames=frame_outputs,
            )
            return sample_output
        else:
            return self.inference_step(inputs, temperature)
