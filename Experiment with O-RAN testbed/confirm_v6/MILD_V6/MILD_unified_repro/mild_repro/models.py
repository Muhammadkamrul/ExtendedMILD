"""
models.py -- MILD (teacher-augmented MoE), its losses, and the MLP/LSTM baselines.

Faithful ports of hybrid_mild_model.py, custom_loss.head_decorrelation and the
loss functions defined in run_hybrid_experiment.py. Two compatibility fixes are
applied so that the code runs on both Keras 2 (TF <= 2.15) and Keras 3
(TF >= 2.16); neither changes the mathematics:

* tf.keras.losses.Reduction.NONE  ->  reduction='none'
* the gate-picking Lambda binds the loop index at definition time
  (lambda t, i=i: ...). Under Keras 2 the original late-binding lambda was
  evaluated once while the graph was built, so the intended index was used.
  Under Keras 3 the functional graph is re-executed and the late-binding
  lambda would silently pick the LAST intent for every expert. Binding the
  index reproduces the Keras 2 behaviour.

The per-head focal term keeps the original tensor shapes, including the
(B,1)*(B,) broadcast inside base_focal, because that is what produced the
paper numbers.
"""
from __future__ import annotations

import tensorflow as tf
from tensorflow.keras import layers, models, regularizers


# ----------------------------------------------------------------------------
# MILD architecture
# ----------------------------------------------------------------------------
def create_mild_moe_with_teacher(num_features, num_teacher, intent_ids, l2=1e-5,
                                 dropout=0.2, gate_units=48, enc_units=(96, 96),
                                 use_teacher_gate=True, gate_mode='multiply', gate_use='scale'):
    """gate_mode = 'multiply' (paper): expert_i x g_i feeds the risk head of intent i.
    gate_mode = 'residual' (v2.3): expert_i x (1 + g_i). In the paper form the gate is trained on ROOT-CAUSE labels,
    so during a co-drift it drives g_victim -> 0 and silences the risk head of the victim intent, although the
    victim intent does fail (its own failure must be predicted). With the residual form the gate still
    re-weights the experts and still gives the root cause, but it can no longer switch a failing intent's head off."""
    if gate_mode not in ('multiply', 'residual') or gate_use not in ('scale', 'attr_only'):
        raise ValueError('Invalid gate_mode or gate_use')
    x_in = layers.Input(shape=(num_features,), name='x')
    inputs = [x_in]
    if use_teacher_gate:
        t_in = layers.Input(shape=(num_teacher,), name='teacher_in')
        inputs.append(t_in)

    x = x_in
    for u in enc_units:
        x = layers.Dense(u, activation='relu', kernel_regularizer=regularizers.l2(l2))(x)
        x = layers.Dropout(dropout)(x)
    shared = x

    gx = layers.Concatenate(name='gate_fuse')([shared, t_in]) if use_teacher_gate else shared
    g = layers.Dense(gate_units, activation='relu')(gx)
    g = layers.Dropout(dropout)(g)
    gate_probs = layers.Dense(len(intent_ids), activation='softmax', name='gate')(g)

    head_hiddens, outputs = [], []
    for i, intent in enumerate(intent_ids):
        e = layers.Dense(64, activation='relu', kernel_regularizer=regularizers.l2(l2),
                         name=f'expert_{intent}_1')(shared)
        e = layers.Dropout(dropout)(e)
        e = layers.Dense(32, activation='relu', kernel_regularizer=regularizers.l2(l2),
                         name=f'expert_{intent}_2')(e)
        gi = layers.Lambda(lambda t, i=i: tf.expand_dims(t[:, i], -1),
                           output_shape=(1,), name=f'gate_pick_{intent}')(gate_probs)
        if gate_mode == 'residual':
            gi = layers.Lambda(lambda t: 1.0 + t, output_shape=(1,), name=f'gate_res_{intent}')(gi)
        if gate_use == 'scale':
            e = layers.Multiply(name=f'gate_mul_{intent}')([e, gi])
        h = layers.Dense(16, activation='relu', name=f'head_{intent}')(e)
        head_hiddens.append(h)
        outputs.append(layers.Dense(1, activation='sigmoid', name=f'{intent}_out')(h))

    concat_hidden = layers.Concatenate(name='head_concat')(head_hiddens)
    return models.Model(inputs=inputs, outputs=outputs + [gate_probs, concat_hidden],
                        name='MILD-MoE-HYBRID')


# ----------------------------------------------------------------------------
# losses
# ----------------------------------------------------------------------------
def _logit(p, eps=1e-6):
    p = tf.clip_by_value(p, eps, 1.0 - eps)
    return tf.math.log(p) - tf.math.log(1.0 - p)


def distillation_focal_with_temperature(alpha=0.2, gamma=2.0, horizon_min=120, pos_weight=3.0,
                                        min_time_weight=0.3, temperature=2.0):
    bce = tf.keras.losses.BinaryCrossentropy(from_logits=False, reduction='none')
    eps = tf.constant(1e-6, tf.float32)

    def base_focal(y_ttf, y_pred):
        is_pos = tf.cast(y_ttf > 0.0, tf.float32)
        ttf = tf.maximum(y_ttf, eps)
        time_w = min_time_weight + (1.0 - min_time_weight) * tf.clip_by_value(ttf / float(horizon_min), 0.0, 1.0)
        cls_w = 1.0 + (pos_weight - 1.0) * is_pos
        w = cls_w * time_w
        per_bce = bce(is_pos, y_pred)
        p_t = y_pred * is_pos + (1.0 - y_pred) * (1.0 - is_pos)
        mod = tf.pow(1.0 - p_t, gamma)
        return tf.reduce_mean(mod * per_bce * tf.stop_gradient(tf.squeeze(w, axis=-1)))

    kld = tf.keras.losses.KLDivergence()

    def loss(y_true_and_soft, y_pred):
        y_true_and_soft = tf.cast(y_true_and_soft, tf.float32)
        y_ttf = tf.slice(y_true_and_soft, [0, 0], [-1, 1])
        y_soft = tf.slice(y_true_and_soft, [0, 1], [-1, 1])
        gt_loss = base_focal(y_ttf, y_pred)
        y_soft_T = tf.sigmoid(_logit(y_soft) / temperature)
        y_pred_T = tf.sigmoid(_logit(y_pred) / temperature)
        distill = kld(tf.concat([y_soft_T, 1.0 - y_soft_T], axis=1),
                      tf.concat([y_pred_T, 1.0 - y_pred_T], axis=1))
        return alpha * gt_loss + (1.0 - alpha) * distill
    return loss


def gate_kldiv_with_teacher(supervise_weight=0.3, teacher_weight=0.15, sparsity_weight=0.005):
    """Gate loss used for every paper result (teacher term is NOT masked)."""
    kld = tf.keras.losses.KLDivergence(reduction='none')

    def loss(y_true_concat, p_gate):
        K = tf.shape(p_gate)[-1]
        y_true_concat = tf.cast(y_true_concat, tf.float32)
        p_gate = tf.cast(p_gate, tf.float32)
        y_gate_true = y_true_concat[:, :K]
        y_gate_teacher = y_true_concat[:, K:]
        s_true = tf.reduce_sum(y_gate_true, axis=-1, keepdims=True)
        y_dist_true = tf.where(s_true > 0, y_gate_true / s_true, y_gate_true)
        mask = tf.cast(s_true > 0, tf.float32)
        s_teacher = tf.reduce_sum(y_gate_teacher, axis=-1, keepdims=True)
        y_dist_teacher = tf.where(s_teacher > 0, y_gate_teacher / s_teacher,
                                  tf.ones_like(y_gate_teacher) / tf.cast(K, tf.float32))
        kl_true = supervise_weight * tf.reduce_mean(kld(y_dist_true, p_gate) * tf.squeeze(mask, -1))
        kl_teacher = teacher_weight * tf.reduce_mean(kld(y_dist_teacher, p_gate))
        sparsity = sparsity_weight * tf.reduce_mean(tf.reduce_sum(p_gate * (1.0 - p_gate), axis=-1))
        return kl_true + kl_teacher + sparsity
    return loss


def head_decorrelation(lambda_d=1e-4):
    def loss(_, h_concat):
        x = h_concat - tf.reduce_mean(h_concat, axis=0, keepdims=True)
        cov = tf.matmul(tf.transpose(x), x) / (tf.cast(tf.shape(x)[0], tf.float32) + 1e-6)
        diag = tf.linalg.tensor_diag(tf.linalg.diag_part(cov))
        return lambda_d * tf.reduce_mean(tf.square(cov - diag))
    return loss


# ----------------------------------------------------------------------------
# neural baselines
# ----------------------------------------------------------------------------
def create_mlp_baseline(num_features, intent_ids, enc_units=(96, 96), head_units=16, l2=1e-5, dropout=0.2):
    x_in = layers.Input(shape=(num_features,), name='x')
    x = x_in
    for u in enc_units:
        x = layers.Dense(u, activation='relu', kernel_regularizer=regularizers.l2(l2))(x)
        x = layers.Dropout(dropout)(x)
    outputs = []
    for intent in intent_ids:
        h = layers.Dense(head_units, activation='relu', name=f'head_{intent}')(x)
        outputs.append(layers.Dense(1, activation='sigmoid', name=f'{intent}_out')(h))
    return models.Model(inputs=x_in, outputs=outputs, name='MLP_Baseline')


def create_lstm_baseline(input_shape, intent_ids, lstm_units=64, dropout=0.2):
    x_in = layers.Input(shape=input_shape, name='x_seq')
    x = layers.LSTM(lstm_units, return_sequences=False)(x_in)
    x = layers.Dropout(dropout)(x)
    shared = layers.Dense(64, activation='relu')(x)
    outputs = []
    for intent in intent_ids:
        h = layers.Dense(16, activation='relu', name=f'head_{intent}')(shared)
        outputs.append(layers.Dense(1, activation='sigmoid', name=f'{intent}_out')(h))
    return models.Model(inputs=x_in, outputs=outputs, name='LSTM_Baseline')
