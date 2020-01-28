import os
import argparse

import ruamel.yaml
import tensorflow as tf
import numpy as np
from sklearn.model_selection import train_test_split

from model.transformer_factory import Combiner
from losses import masked_crossentropy, masked_mean_squared_error
from utils import buffer_mel

np.random.seed(42)
tf.random.set_seed(42)

parser = argparse.ArgumentParser()
parser.add_argument('--meldir', dest='mel_dir', type=str)
parser.add_argument('--metafile', dest='metafile', type=str)
parser.add_argument('--logdir', dest='log_dir', type=str)
parser.add_argument('--config', dest='config', type=str)
args = parser.parse_args()

yaml = ruamel.yaml.YAML()
config = yaml.load(open(args.config, 'r'))
args.log_dir = os.path.join(args.log_dir, os.path.splitext(os.path.basename(args.config))[0])
os.makedirs(args.log_dir, exist_ok=True)
# current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
weights_paths = os.path.join(args.log_dir, f'weights/')
os.makedirs(weights_paths, exist_ok=True)
weights_paths = {}
weights_paths_alt = {}
for kind in config['transformer_kinds']:
    weights_paths[kind] = os.path.join(args.log_dir, f'weights/{kind}/')
    weights_paths_alt[kind] = os.path.join(args.log_dir, f'weights/')
    # os.makedirs(weights_paths, exist_ok=True)


def norm_tensor(tensor):
    return tf.math.divide(
        tf.math.subtract(
            tensor,
            tf.math.reduce_min(tensor)
        ),
        tf.math.subtract(
            tf.math.reduce_max(tensor),
            tf.math.reduce_min(tensor)
        )
    )


def plot_attention(outputs, step, info_string=''):
    for k in outputs['attention_weights'].keys():
        for i in range(len(outputs['attention_weights'][k][0])):
            image_batch = norm_tensor(tf.expand_dims(outputs['attention_weights'][k][:, i, :, :], -1))
            tf.summary.image(info_string + k + f' head{i}', image_batch,
                             step=step)


def display_mel(pred, step, info_string='', sr=22050):
    img = tf.transpose(tf.exp(pred))
    buf = buffer_mel(img, sr=sr)
    img_tf = tf.image.decode_png(buf.getvalue(), channels=3)
    img_tf = tf.expand_dims(img_tf, 0)
    tf.summary.image(info_string, img_tf, step=step)


def get_norm_mel(mel_path, start_vec, end_vec, divisible_by=1):
    mel = np.load(mel_path)
    norm_mel = np.log(mel.clip(1e-5))
    norm_mel = np.concatenate([start_vec, norm_mel, end_vec])
    return norm_mel


start_vec = np.ones((1, config['mel_channels'])) * -3
end_vec = np.ones((1, config['mel_channels']))
mel_text_stop_samples = []
count = 0
alphabet = set()

print('Loading data')
with open(str(args.metafile), 'r', encoding='utf-8') as f:
    for l in f.readlines():
        l_split = l.split('|')
        text = l_split[-1].strip().lower()
        mel_file = os.path.join(str(args.mel_dir), l_split[0] + '.npy')
        norm_mel = get_norm_mel(mel_file, start_vec, end_vec)
        stop_probs = np.ones(norm_mel.shape[0], dtype=np.int64)
        stop_probs[-1] = 2
        mel_text_stop_samples.append((norm_mel, text, stop_probs))
        alphabet.update(list(text))
        count += 1
        if count > config['n_samples']:
            break

print('Creating model')
combiner = Combiner(
    config=config,
    tokenizer_alphabet=sorted(list(alphabet)))

loss_coeffs = [1.0, 1.0, 1.0]
combiner.transformers['mel_to_text'].compile(loss=masked_crossentropy,
                                             optimizer=tf.keras.optimizers.Adam(config['learning_rate'], beta_1=0.9,
                                                                                beta_2=0.98,
                                                                                epsilon=1e-9))
combiner.transformers['text_to_text'].compile(loss=masked_crossentropy,
                                              optimizer=tf.keras.optimizers.Adam(config['learning_rate'], beta_1=0.9,
                                                                                 beta_2=0.98,
                                                                                 epsilon=1e-9))
combiner.transformers['mel_to_mel'].compile(loss=[masked_mean_squared_error,
                                                  masked_crossentropy,
                                                  masked_mean_squared_error],
                                            loss_weights=loss_coeffs,
                                            optimizer=tf.keras.optimizers.Adam(config['learning_rate'], beta_1=0.9,
                                                                               beta_2=0.98,
                                                                               epsilon=1e-9))
combiner.transformers['text_to_mel'].compile(loss=[masked_mean_squared_error,
                                                   masked_crossentropy,
                                                   masked_mean_squared_error],
                                             loss_weights=loss_coeffs,
                                             optimizer=tf.keras.optimizers.Adam(config['learning_rate'], beta_1=0.9,
                                                                                beta_2=0.98,
                                                                                epsilon=1e-9))
    
print('Dumping config.')
print(config)
yaml.dump(config, open(os.path.join(args.log_dir, os.path.basename(args.config)), 'w'))
print('Creating dataset')
train_list, test_list = train_test_split(mel_text_stop_samples, test_size=100, random_state=42)


def encode_text(text, tokenizer):
    encoded_text = tokenizer.encode(text)
    return [tokenizer.start_token_index] + encoded_text + [tokenizer.end_token_index]


tokenized_train_list = [(mel, encode_text(text, combiner.tokenizer), stop_prob)
                        for mel, text, stop_prob in train_list]
tokenized_test_list = [(mel, encode_text(text, combiner.tokenizer), stop_prob)
                       for mel, text, stop_prob in test_list]

train_set_generator = lambda: (item for item in tokenized_train_list)
train_dataset = tf.data.Dataset.from_generator(train_set_generator,
                                               output_types=(tf.float32, tf.int64, tf.int64))
train_dataset = train_dataset.shuffle(1000).padded_batch(
    config['batch_size'], padded_shapes=([-1, 80], [-1], [-1]), drop_remainder=True)

losses = {}
summary_writers = {}

for kind in config['transformer_kinds']:
    summary_writers[kind] = tf.summary.create_file_writer(os.path.join(args.log_dir, f'{kind}'))
    losses[kind] = []


def linear_dropout_schedule(step):
    mx = config['decoder_prenet_dropout_schedule_max']
    mn = config['decoder_prenet_dropout_schedule_min']
    max_steps = config['decoder_prenet_dropout_schedule_max_steps']
    dout = max(((-mx + mn) / max_steps) * step + mx, mn)
    return tf.cast(dout, tf.float32)


def linear_schedule(step, mx ,mn, max_steps):
    dout = ((-mx + mn) / max_steps) * step + mx
    return tf.cast(dout, tf.float32)


def linear_peak(step, starting_lr, max_lr, switch_step, max_step, min_lr):
    if step < switch_step:
        dout = min(linear_schedule(step, mx=starting_lr, mn=max_lr, max_steps=switch_step), max_lr)
    else:
        dout = max(linear_schedule(-switch_step + step, mx=max_lr, mn=min_lr, max_steps=max_step-switch_step), min_lr)
    return dout


def random_mel_mask(tensor, mask_prob):
    tensor_shape = tf.shape(tensor)
    mask_floats = tf.random.uniform((tensor_shape[0], tensor_shape[1]))
    mask = tf.cast(mask_floats > mask_prob, tf.float32)
    mask = tf.expand_dims(mask, -1)
    mask = tf.broadcast_to(mask, tensor_shape)
    masked_tensor = tensor * mask
    return masked_tensor


def random_text_mask(tensor, mask_prob):
    tensor_shape = tf.shape(tensor)
    mask_floats = tf.random.uniform((tensor_shape[0], tensor_shape[1]))
    mask = tf.cast(mask_floats > mask_prob, tf.int64)
    masked_tensor = tensor * mask
    return masked_tensor


def learning_rate_schedule(step):
    if (config['resume_from'] - step) < config['warmup_steps']:
        return config['warmup_lr']
    else:
        return config['learning_rate']


def set_learning_rate(step):
    lr = learning_rate_schedule(step)
    print(f'setting learning rate to {lr}')
    for kind in config['transformer_kinds']:
        combiner.transformers[kind].optimizer.lr.assign(lr)


decoder_prenet_dropout = config['fixed_decoder_prenet_dropout']

checkpoints = {}
managers = {}
for kind in config['transformer_kinds']:
    # here step could be config['batch_size'] instead
    checkpoints[kind] = tf.train.Checkpoint(step=tf.Variable(1), optimizer=combiner.transformers[kind].optimizer,
                                            net=combiner.transformers[kind])
    managers[kind] = tf.train.CheckpointManager(checkpoints[kind], weights_paths[kind],
                                                max_to_keep=config['keep_n_weights'])
    
    checkpoints[kind].restore(managers[kind].latest_checkpoint)
    if managers[kind].latest_checkpoint:
        print(f'Restored {kind} from {managers[kind].latest_checkpoint}')
    else:
        print(f'Initializing {kind} from scratch.')

# this is deprecated, only to load old weights. Should use checkpoints
if config['resume_from']:
    print(f'Loading weights at step {config["resume_from"]}.')
    combiner.load_weights(path=weights_paths_alt, steps=config['resume_from'])
    for kind in config['transformer_kinds']:
        checkpoints[kind].step.assign_add(int(config['resume_from']))

print('Starting training')
for epoch in range(config['epochs']):
    print(f'Epoch {epoch}')
    for (batch, (mel, text, stop)) in enumerate(train_dataset):
        if config['use_decoder_prenet_dropout_schedule']:
            decoder_prenet_dropout = linear_dropout_schedule(int(checkpoints[config['transformer_kinds'][0]].step))
        set_learning_rate(int(checkpoints[config['transformer_kinds'][0]].step))
        output = combiner.train_step(text=text,
                                     mel=mel,
                                     stop=stop,
                                     speech_decoder_prenet_dropout=decoder_prenet_dropout,
                                     mask_prob=config['mask_prob'],
                                     )
        print(f'\nbatch {int(checkpoints[config["transformer_kinds"][0]].step)}')
        
        # CHECKPOINTING TODO: REMOVE step= config['resume_from'] + 
        for kind in config['transformer_kinds']:
            checkpoints[kind].step.assign_add(1)
            losses[kind].append(float(output[kind]['loss']))
            with summary_writers[kind].as_default():
                if (kind == 'text_to_mel') or (kind == 'mel_to_mel'):
                    for k in output[kind]['losses'].keys():
                        tf.summary.scalar(kind + '_' + k, output[kind]['losses'][k],
                                          step=config['resume_from'] + combiner.transformers[kind].optimizer.iterations)
                tf.summary.scalar('loss', output[kind]['loss'],
                                  step=config['resume_from'] + combiner.transformers[kind].optimizer.iterations)
            print(f'{kind} mean loss: {sum(losses[kind]) / len(losses[kind])}')
            
            if int(checkpoints[kind].step) % config['weights_save_freq'] == 0:
                save_path = managers[kind].save()
                print(f'Saved checkpoint for step {int(checkpoints[kind].step)}: {save_path}')
            
            if int(checkpoints[kind].step) % config['plot_attention_freq'] == 0:
                with summary_writers[kind].as_default():
                    plot_attention(output[kind],
                                   step=config['resume_from'] + combiner.transformers[kind].optimizer.iterations,
                                   info_string='train attention ')
        
        with summary_writers[config['transformer_kinds'][0]].as_default():
            tf.summary.scalar('dropout', decoder_prenet_dropout,
                              step=config['resume_from'] + combiner.transformers[
                                  config['transformer_kinds'][0]].optimizer.iterations)
            tf.summary.scalar('learning_rate', combiner.transformers[config['transformer_kinds'][0]].optimizer.lr,
                              step=config['resume_from'] + combiner.transformers[
                                  config['transformer_kinds'][0]].optimizer.iterations)
        
        if ('mel_to_mel' in config['transformer_kinds']) and ('text_to_mel' in config['transformer_kinds']):
            if int(checkpoints[config['transformer_kinds'][0]].step) % config['image_freq'] == 0:
                pred = {}
                test_val = {}
                for i in range(0, 2):
                    mel_target = test_list[i][0]
                    max_pred_len = mel_target.shape[0] + 50
                    test_val['text_to_mel'] = combiner.tokenizer.encode(test_list[i][1])
                    test_val['mel_to_mel'] = mel_target
                    for kind in ['text_to_mel', 'mel_to_mel']:
                        pred[kind] = combiner.transformers[kind].predict(test_val[kind],
                                                                         max_length=max_pred_len,
                                                                         decoder_prenet_dropout=0.5)
                        with summary_writers[kind].as_default():
                            plot_attention(pred[kind], step=config['resume_from'] + combiner.transformers[
                                kind].optimizer.iterations,
                                           info_string='test attention ')
                            display_mel(pred[kind]['mel'], step=config['resume_from'] + combiner.transformers[kind].optimizer.iterations,
                                        info_string='test mel {}'.format(i))
                            display_mel(mel_target, step=config['resume_from'] + combiner.transformers[
                                'mel_to_mel'].optimizer.iterations,
                                        info_string='target mel {}'.format(i))
        
        if ('mel_to_text' in config['transformer_kinds']) and ('text_to_text' in config['transformer_kinds']):
            if int(checkpoints[config['transformer_kinds'][0]].step) % config['text_freq'] == 0:
                pred = {}
                test_val = {}
                for i in range(0, 2):
                    test_val['mel_to_text'] = test_list[i][0]
                    test_val['text_to_text'] = combiner.tokenizer.encode(test_list[i][1])
                    decoded_target = combiner.tokenizer.decode(test_val['text_to_text'])
                    for kind in ['mel_to_text', 'text_to_text']:
                        pred[kind] = combiner.transformers[kind].predict(test_val[kind])
                        pred[kind] = combiner.tokenizer.decode(pred[kind]['output'])
                        with summary_writers[kind].as_default():
                            tf.summary.text(f'{kind} from validation',
                                            f'(pred) {pred[kind]}\n(target) {decoded_target}',
                                            step=config['resume_from'] + combiner.transformers[
                                                kind].optimizer.iterations)
