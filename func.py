import tensorflow as tf
import tensorflow.contrib as tc

def softmax(value, mask):
    exp = tf.exp(value) * mask
    alpha = exp / tf.expand_dims(tf.reduce_sum(exp, -1), -1)
    return alpha


def dense(value, last_dim, use_bias=True, scope='dense'):
    with tf.variable_scope(scope):
        weight = tf.get_variable('weight', [value.get_shape()[-1], last_dim])
        out = tf.einsum('aij,jk->aik', value, weight)
        if use_bias:
            b = tf.get_variable('bias', [last_dim])
            out += b
        out = tf.identity(out, 'dense')
        return out


def attention_pooling(value, hidden_dim, mask):
    sj = tf.nn.tanh(dense(value, hidden_dim, scope='summary_sj'))
    uj = tf.squeeze(dense(sj, 1, use_bias=False, scope='summary_uj'), [-1])#[batch, len]
    alpha = softmax(uj, mask)#[batch, len]
    return tf.reduce_sum(tf.expand_dims(alpha, axis=-1) * value, axis=1), alpha


def summary(value, hidden_dim, mask, keep_prob, scope='summary'):
    with tf.variable_scope(scope):
        value = tf.nn.dropout(value, keep_prob=keep_prob)
        s, _ = attention_pooling(value, hidden_dim, mask)
        return s


def pointer(encoder_state, decoder_state, hidden_dim, mask, scope='pointer'):
    with tf.variable_scope(scope):
        length = tf.shape(encoder_state)[1]
        tiled_decoder_state = tf.tile(tf.expand_dims(decoder_state, axis=1), [1, length, 1])
        united_state = tf.concat([tiled_decoder_state, encoder_state], axis=2)
        _, alpha = attention_pooling(united_state, hidden_dim, mask)
        next_decoder_state = tf.reduce_sum(tf.expand_dims(alpha, axis=-1) * encoder_state, axis=1)
        return next_decoder_state, alpha


def cross_entropy(logit, target, mask, pos_weight=1.0):
    logit = tf.clip_by_value(logit, 1E-18, 1-1E-18)
    loss_t = -target * tf.log(logit) * pos_weight * mask
    loss_f = -(1-target) * tf.log(1-logit) * mask
    return loss_t + loss_f


def sparse_cross_entropy(logit, target, mask):
    one_hot = tf.one_hot(target, tf.shape(logit)[-1], dtype=tf.float32)
    loss = cross_entropy(logit, one_hot, mask)
    return tf.reduce_sum(loss, axis=-1)


def tensor_to_mask(value):
    mask = tf.cast(value, tf.bool)
    return tf.cast(mask, tf.float32), tf.reduce_sum(tf.cast(mask, tf.int32), axis=-1)


def rnn(rnn_type, inputs, length, hidden_size, layer_num=1, dropout_keep_prob=None, concat=True):
    """
    Implements (Bi-)LSTM, (Bi-)GRU and (Bi-)RNN
    Args:
        rnn_type: the type of rnn
        inputs: padded inputs into rnn
        length: the valid length of the inputs
        hidden_size: the size of hidden units
        layer_num: multiple rnn layer are stacked if layer_num > 1
        dropout_keep_prob:
        concat: When the rnn is bidirectional, the forward outputs and backward outputs are
                concatenated if this is True, else we add them.
    Returns:
        RNN outputs and final state
    """
    if not rnn_type.startswith('bi'):
        cell = get_cell(rnn_type, hidden_size, layer_num, dropout_keep_prob)
        outputs, states = tf.nn.dynamic_rnn(cell, inputs, sequence_length=length, dtype=tf.float32)
        if rnn_type.endswith('lstm'):
            #c = [state.c for state in states]
            h = [state.h for state in states]
            states = h
    else:
        cell_fw = get_cell(rnn_type, hidden_size, layer_num, dropout_keep_prob)
        cell_bw = get_cell(rnn_type, hidden_size, layer_num, dropout_keep_prob)
        outputs, states = tf.nn.bidirectional_dynamic_rnn(
            cell_bw, cell_fw, inputs, sequence_length=length, dtype=tf.float32
        )
        states_fw, states_bw = states
        if rnn_type.endswith('lstm'):
            #c_fw = [state_fw.c for state_fw in states_fw]
            h_fw = [state_fw.h for state_fw in states_fw]
            #c_bw = [state_bw.c for state_bw in states_bw]
            h_bw = [state_bw.h for state_bw in states_bw]
            states_fw, states_bw = h_fw, h_bw
        if concat:
            outputs = tf.concat(outputs, -1)
            states = tf.concat([tf.concat(states_fw, -1), tf.concat(states_bw, -1)], -1)
        else:
            outputs = outputs[0] + outputs[1]
            states = states_fw + states_bw
    return outputs, states


def get_cell(rnn_type, hidden_size, layer_num=1, dropout_keep_prob=None):
    """
    Gets the RNN Cell
    Args:
        rnn_type: 'lstm', 'gru' or 'rnn'
        hidden_size: The size of hidden units
        layer_num: MultiRNNCell are used if layer_num > 1
        dropout_keep_prob: dropout in RNN
    Returns:
        An RNN Cell
    """
    cells = []
    for i in range(layer_num):
        if rnn_type.endswith('lstm'):
            cell = tc.rnn.LSTMCell(num_units=hidden_size, state_is_tuple=True)
        elif rnn_type.endswith('gru'):
            cell = tc.rnn.GRUCell(num_units=hidden_size)
        elif rnn_type.endswith('rnn'):
            cell = tc.rnn.BasicRNNCell(num_units=hidden_size)
        else:
            raise NotImplementedError('Unsuported rnn type: {}'.format(rnn_type))
        if dropout_keep_prob is not None:
            cell = tc.rnn.DropoutWrapper(cell,
                                         input_keep_prob=dropout_keep_prob,
                                         output_keep_prob=dropout_keep_prob)
        cells.append(cell)
    cells = tc.rnn.MultiRNNCell(cells, state_is_tuple=True)
    return cells


def dot_attention(value, memory, mask, weight_dim, keep_prob):
    value = tf.nn.dropout(value, keep_prob)#[batch, plen, 500]
    memory = tf.nn.dropout(memory, keep_prob)#[batch, qlen, 500]
    dense_value = dense(value, weight_dim, False, 'value')#[batch, plen, 75]
    dense_memory = dense(memory, weight_dim, False, 'memory')#[batch, qlen, 75]
    coref = tf.matmul(dense_value, tf.transpose(dense_memory, [0, 2, 1])) / (weight_dim**0.5)#[batch, plen, qlen]
    alpha = softmax(coref, tf.expand_dims(mask, axis=1))#[batch, plen, qlen]
    ct = tf.matmul(alpha, memory, name='ct')
    return ct