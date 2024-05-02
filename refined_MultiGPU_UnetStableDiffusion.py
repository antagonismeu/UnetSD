import os
try:
    import tensorflow as tf
    if tf.__version__.startswith('1'):
        raise ImportError("Please upgrade your TensorFlow to version 2.x")
    from tensorflow.keras.layers import *
    from tensorflow.keras import layers
    from tensorflow.keras.models import Model
    from tensorflow.keras.optimizers.legacy import Adam
    from tensorflow.keras.losses import MeanSquaredError
    import pandas as pd
    import pickle, argparse
    from PIL import Image
    import numpy as np
except :
    requirements = ['numpy', 'tensorflow', 'pandas', 'Pillow', 'transformers']
    for item in requirements :
        os.system(f'pip3 install {item}')
        print('Done!')

gpu_devices = tf.config.experimental.list_physical_devices('GPU')
if gpu_devices:
    for device in gpu_devices:
        tf.config.experimental.set_memory_growth(device, True)



global WIDTH, HEIGHT, CHANNEL
width, height = 256, 256                                  #INFERIOR BOUNDARY : width, height = 128, 128  
WIDTH , HEIGHT = width, height
BATCH_SIZE = 4
channel = 3
CHANNEL = channel
GLOBAL_BATCH_SIZE = BATCH_SIZE * tf.distribute.MirroredStrategy().num_replicas_in_sync




def configuration() :
    os.makedirs('./log', exist_ok=True)
    os.makedirs('./models', exist_ok=True)
    os.makedirs('./samples', exist_ok=True)
    os.makedirs('./temporary_checkpoints', exist_ok=True)


class TextEncoder(tf.keras.Model):
    def __init__(self, vocab_size, output_dim=128, embed_dim=512):
        super(TextEncoder, self).__init__()
        self.embedding = Embedding(input_dim=vocab_size, output_dim=embed_dim)
        self.text_projection = Dense(output_dim, activation='relu')


    def call(self, input_ids):
        outputs = self.embedding(input_ids)
        text_embeddings = self.text_projection(outputs)
        return text_embeddings


class ImageEncoder(tf.keras.Model):
    def __init__(self, input_shape=(WIDTH, HEIGHT, 3), output_dim=512):
        super(ImageEncoder, self).__init__()
        self.conv_blocks = [
            layers.Conv2D(64, (3, 3), activation='relu', padding='same', input_shape=input_shape),
            layers.MaxPooling2D((2, 2)),
            layers.Conv2D(128, (3, 3), activation='relu', padding='same'),
            layers.MaxPooling2D((2, 2)),
            layers.Conv2D(256, (4, 4), activation='relu', padding='same'),
            layers.MaxPooling2D((2, 2)),
            layers.Conv2D(512, (4, 4), activation='relu', padding='same'),
            layers.MaxPooling2D((2, 2)),
            layers.Conv2D(1024, (4, 4), activation='relu', padding='same'),
            layers.MaxPooling2D((2, 2)),
        ]
        self.flatten = layers.Flatten()
        self.image_projection = layers.Dense(output_dim)
        self.output_dim = output_dim

    def call(self, inputs):
        x = inputs
        for layer in self.conv_blocks:
            x = layer(x)
        x = self.flatten(x)
        latent_representation = self.image_projection(x)
        return latent_representation





class MultiHeadAttention(layers.Layer):
    def __init__(self, d_model, num_heads):
        super(MultiHeadAttention, self).__init__()
        self.num_heads = num_heads
        self.d_model = d_model
        
        assert d_model % self.num_heads == 0
        
        self.depth = d_model // self.num_heads
        
        self.wq = layers.Dense(d_model)
        self.wk = layers.Dense(d_model)
        self.wv = layers.Dense(d_model)
        
        self.dense = layers.Dense(d_model)
        
    def split_heads(self, x, batch_size):
        x = tf.reshape(x, (batch_size, -1, self.num_heads, self.depth))
        return tf.transpose(x, perm=[0, 2, 1, 3])
    
    def call(self, q, k, v, mask=None):
        batch_size = tf.shape(q)[0]

        q = self.apply_attention(q, mask)
        k = self.apply_attention(k, mask)
        v= self.apply_attention(v, mask)
        
        q = self.wq(q)
        k = self.wk(k)
        v = self.wv(v)
        
        q = self.split_heads(q, batch_size)
        k = self.split_heads(k, batch_size)
        v = self.split_heads(v, batch_size)
        
        scaled_attention, attention_weights = self.scaled_dot(q, k, v)
        
        scaled_attention = tf.transpose(scaled_attention, perm=[0, 2, 1, 3])
        concat_attention = tf.reshape(scaled_attention, (batch_size, self.d_model))
        
        output = self.dense(concat_attention)
        
        return output, attention_weights
    

    def apply_attention(self, text_inputs, mask) :
        if mask is None:
            mask = tf.cast(tf.math.equal(text_inputs, 0), tf.float32) 
            text_inputs += (mask * -1e9)
        return text_inputs    


    def scaled_dot(self, q, k, v):
        matmul_qk = tf.matmul(q, k, transpose_b=True)
        
        dk = tf.cast(tf.shape(k)[-1], tf.float32)
        scaled_attention_logits = matmul_qk / tf.math.sqrt(dk)
        
            
        attention_weights = tf.nn.softmax(scaled_attention_logits, axis=-1)
        
        output = tf.matmul(attention_weights, v)
        
        return output, attention_weights




class UNetDiffusionModule(tf.keras.Model):
    def __init__(self, num_batch, width, height, text_embedding_dim=128, num_heads=8, d_model=512):
        super(UNetDiffusionModule, self).__init__()
        self.batch_size = num_batch 
        self.width = width
        self.height = height          
        self.num_heads = num_heads
        self.d_model = d_model
        self.multi_head_attention = MultiHeadAttention(d_model=self.d_model, num_heads=self.num_heads)     
        self.add = Add()
        self.activation = Activation('relu')
        self.batch_nomalization = BatchNormalization()
        self.compression = Dense(512)           #latent_image[-1]
        self.concatenate = Concatenate()
        self.exaggeration = Conv2D(32, kernel_size=7, strides=1, padding='same', activation='sigmoid')
        self.flatten = Flatten()

        self.conv_blocks1 = [
            Conv2D(64, 4, strides=2, padding='same'),
            BatchNormalization(),
            self.activation
        ]
        self.conv_blocks2 = [
            Conv2D(128, 4, strides=2, padding='same'),
            BatchNormalization(),
            self.activation
        ]
        self.conv_blocks3 = [
            Conv2D(256, 4, strides=2, padding='same'),
            BatchNormalization(),
            self.activation
        ]
        self.deconv_blocks1 = [
            Conv2DTranspose(64, 4, strides=2, padding='same'),
            BatchNormalization(),
            self.activation
        ]
        self.deconv_blocks1_ = [
            Conv2DTranspose(64, 4, strides=2, padding='same'),
            BatchNormalization(),
            self.activation
        ]
        self.deconv_blocks2 = [
            Conv2DTranspose(128, 4, strides=2, padding='same'),
            BatchNormalization(),
            self.activation
        ]
        self.residual_block1 = [
            Conv2D(128, 3, strides=1, padding='same'),
            BatchNormalization(),
            self.activation,
            Conv2D(128, 3, strides=1, padding='same'),
            BatchNormalization()
        ]
        self.residual_block2 = [
            Conv2D(256, 3, strides=1, padding='same'),
            BatchNormalization(),
            self.activation,
            Conv2D(256, 3, strides=1, padding='same'),
            BatchNormalization()
        ]



    def call(self, noisy_images, time_embedding, text_embedding):
            
        weighted_latent_vector, _ = self.multi_head_attention(noisy_images, text_embedding, text_embedding)
        eigenvector = Concatenate(axis=-1)([weighted_latent_vector, time_embedding])
        dim_1 = tf.TensorShape(eigenvector.shape).as_list()[0]
        dim_2 = tf.TensorShape(eigenvector.shape).as_list()[-1]
        dim_3 = tf.TensorShape(noisy_images.shape).as_list()[-1]
            
        eigenvector_reshaped = tf.reshape(eigenvector, [dim_1, 1, 1, dim_2])
            
        d1 = tf.tile(eigenvector_reshaped, [1, self.width, self.height, 1])

        for layer in self.conv_blocks1 :
            d1 = layer(d1)
        d2 = d1
        for layer in self.conv_blocks2 :
            d2 = layer(d2)
        d2_ = d2
        for layer in self.residual_block1 :
            d2_ = layer(d2_)
        d2 = self.add([d2, d2_])
        d2 = self.activation(d2)
        d2_ = d2
        for layer in self.residual_block1 :
            d2_ = layer(d2_)
        d2 = self.add([d2, d2_])
        d2 = self.activation(d2)
        d3 = d2
        for layer in self.conv_blocks3 :
            d3 = layer(d3)
        d3_ = d3
        for layer in self.residual_block2 :
            d3_ = layer(d3_)
        d3 = self.add([d3, d3_])
        d3 = self.activation(d3)
        d3_ = d3        
        for layer in self.residual_block2 :
            d3_ = layer(d3_)
        d3 = self.add([d3, d3_])
        d3 = self.activation(d3)
        d4 = d3
        for layer in self.deconv_blocks2 :
            d4 = layer(d4)
        d4 = self.concatenate([d4, d2])
        d4_ = d4
        for layer in self.residual_block2 :
            d4_ = layer(d4_)
        d4 = self.add([d4, d4_])
        d4 = self.activation(d4)
        d4_ = d4
        for layer in self.residual_block2 :
            d4_ = layer(d4_)
        d4 = self.add([d4, d4_])
        d4 = self.activation(d4)
        d5 = d4 
        for layer in self.deconv_blocks1 :
            d5 = layer(d5) 
        d5 = self.concatenate([d5, d1])
        d5_ = d5
        for layer in self.residual_block1 :
            d5_ = layer(d5_)
        d5 = self.add([d5, d5_])
        d5 = self.activation(d5)
        d5_ = d5
        for layer in self.residual_block1 :
            d5_ = layer(d5_)
        d5 = self.add([d5, d5_])
        d5 = self.activation(d5)
        d6 = d5
        for layer in self.deconv_blocks1_ :
            d6 = layer(d6) 
        d6 = self.exaggeration(d6)
        d6 = self.flatten(d6)
        outputs = self.compression(d6)                            
        return outputs



class ImageDecoder(tf.keras.Model):
    def __init__(self, output_channels):
        super(ImageDecoder, self).__init__()
        self.deconv_blocks = [
            Conv2DTranspose(256, (4, 4), strides=(2, 2), padding='same', activation='relu'),
            BatchNormalization(),
            Conv2DTranspose(128, (4, 4), strides=(2, 2), padding='same', activation='relu'),
            BatchNormalization(),
            Conv2DTranspose(64, (4, 4), strides=(2, 2), padding='same', activation='relu'),
            BatchNormalization(),
            Conv2DTranspose(32, (4, 4), strides=(2, 2), padding='same', activation='relu'),         
            BatchNormalization(),
            Conv2DTranspose(output_channels, (4, 4), strides=(2, 2), padding='same', activation='sigmoid'),
            BatchNormalization()
        ]

    def call(self, inputs):
        inputs = tf.reshape(inputs, (tf.TensorShape(inputs.shape).as_list()[0], 1, 1, tf.TensorShape(inputs.shape).as_list()[-1]))
        tiled_inputs = tf.tile(inputs, [1, 8, 8, 1])        
        x = tiled_inputs
        for layer in self.deconv_blocks:
            x = layer(x)
        return x



    
class Text2ImageDiffusionModel(tf.keras.Model):
    def __init__(self, vocab_size, num_batch, width, height, channel, alpha, input_shape = 512):
        super(Text2ImageDiffusionModel, self).__init__()
        self.text_encoder = TextEncoder(vocab_size)
        self.image_encoder = ImageEncoder()
        self.batch_size = num_batch
        self.time_embedding = Embedding(input_dim=input_shape, output_dim=self.batch_size)
        self.per_time_embedding = Embedding(input_dim=self.batch_size, output_dim=self.batch_size**2)
        self.flatten = Flatten()
        self.width = width
        self.channel = channel
        self.height = height
        self.alpha = alpha
        self.diffusion_module = UNetDiffusionModule(self.batch_size, self.width//32, self.height//32)
        
    def call(self, text_inputs, image_inputs, time_steps):
        text_embeddings = self.text_encoder(text_inputs)
        time_steps_vector = self.time_embedding(time_steps)
        latent_images = self.image_encoder(image_inputs)
        generated_images_list = []
        gaussian_list = []
        generated_images_list.append(latent_images)
        for index in range(len(time_steps_vector) - 1) :
            gaussian_vector = tf.random.normal(shape=(self.batch_size, self.width, self.height, self.channel), mean=0.0, stddev=1.0)
            latent_gaussian_vector = self.image_encoder(gaussian_vector)
            latent_images = np.sqrt(self.alpha**float(index)) * latent_images + np.sqrt(1 - self.alpha**float(index)) * latent_gaussian_vector
            generated_images_list.append(latent_images)
            gaussian_list.append(latent_gaussian_vector)
        images_tensor = tf.stack(generated_images_list, axis=0)
        latent_gaussian_tensor  = tf.stack(gaussian_list, axis=0)
        generated_list = []
        generated_images = self.diffusion_module(images_tensor[-1], self.per_time_embedding(time_steps_vector[-1]), self.flatten(text_embeddings))
        generated_list.append(generated_images)
        varied_tensor = 1/np.sqrt(self.alpha)*(images_tensor[-1] - (1 - self.alpha)/np.sqrt(1 - self.alpha**float(len(images_tensor))) * generated_images) + np.sqrt(1 - self.alpha) * latent_gaussian_tensor[len(images_tensor) - 2]
        for index in reversed(range(1, len(images_tensor) - 1)) :
            generated_images = self.diffusion_module(varied_tensor, self.per_time_embedding(time_steps_vector[index]), self.flatten(text_embeddings))
            generated_list.append(generated_images)
            varied_tensor = 1/np.sqrt(self.alpha)*(varied_tensor - (1 - self.alpha)/np.sqrt(1 - self.alpha**float(index)) * generated_images) + np.sqrt(1 - self.alpha) * latent_gaussian_tensor[index - 1]
        generated_tensor = tf.stack(generated_list, axis=0)
        return varied_tensor, generated_tensor, latent_gaussian_tensor
    

    def compute_loss(self, labels, predictions, loss_fn, l2_reg_coeff=0.01):
        per_example_loss = loss_fn(labels[..., None], predictions[..., None])
        loss = tf.nn.compute_average_loss(per_example_loss)

        
        l2_loss = tf.add_n([tf.nn.l2_loss(v) for v in self.trainable_variables])
        regularization_loss = l2_reg_coeff * l2_loss

        total_loss = loss + regularization_loss

        
        total_loss /= tf.cast(tf.reduce_prod(tf.shape(labels)[:]), tf.float32)

        return total_loss


    
    def train_step(self, optimizer, targets, generated_images, loss_fn):
        with tf.GradientTape() as tape:
            scaled_loss = self.compute_loss(targets, generated_images, loss_fn)
        gradients = tape.gradient(scaled_loss, self.trainable_variables)
        optimizer.apply_gradients(zip(gradients, self.trainable_variables))
        return scaled_loss

        



def load_dataset(description_file, image_directory, batch_size, height, width):
    df = pd.read_csv(description_file)

    image_paths = [f"{image_directory}/{image_id}" for image_id in df['image_id']]
    descriptions = [desc.replace('"', '') for desc in df['description']]

    def preprocess_image(image_path):
        img = tf.io.read_file(image_path)
        img = tf.image.decode_png(img, channels=3)  
        img = tf.image.resize(img, [width, height])
        img_array = tf.image.convert_image_dtype(img, tf.float32) / 127.5 - 1.0
        return img_array
        
    def preprocess_text(descriptions):
        global tokenizer
        tokenizer = tf.keras.preprocessing.text.Tokenizer(filters="")
        tokenizer.fit_on_texts(descriptions)
	
        voc_li = tokenizer.texts_to_sequences(descriptions)

        voc_li = tf.keras.preprocessing.sequence.pad_sequences(voc_li, padding="post")


        text_dataset = tf.data.Dataset.from_tensor_slices(voc_li)        
        return text_dataset, voc_li
        
    image_dataset = tf.data.Dataset.from_tensor_slices(image_paths).map(preprocess_image)
    text_dataset, voc_li = preprocess_text(descriptions)

    dataset = tf.data.Dataset.zip((image_dataset, text_dataset))
    dataset = dataset.shuffle(buffer_size=max(len(df)+1, 512), reshuffle_each_iteration=True).batch(batch_size)
    return dataset,  len(tokenizer.word_index) + 1, voc_li


def generate_image_from_text(sentence, model1, model2, width, height, time_steps, path, gross_range, signature, initial_image=None):
    try :                       
        inputs = [tokenizer.word_index[i] for i in sentence.split(" ")]
        inputs = tf.keras.preprocessing.sequence.pad_sequences([inputs], maxlen=gross_range, padding="post")
        inputs = tf.convert_to_tensor(inputs)

        if initial_image is None:
            initial_image = tf.random.normal(shape=[1, width, height, 3])  
            initial_image = tf.clip_by_value(initial_image, -1, 1)
        def postprocedure(img, path, signature) :
            img = np.clip(img, 0, 255).astype(np.uint8)
            Image.fromarray(img).save(f'{path}/{signature}.png')
        time_steps_ = tf.range(0, time_steps, dtype=tf.float32)
        text_embeddings = model1.text_encoder(inputs)
        time_steps_vector = model1.time_embedding(time_steps_)
        latent_images = model1.image_encoder(initial_image)                    
        generated_gaussian = model1.diffusion_module(latent_images, time_steps_vector[-1], text_embeddings)
        varied_tensor = 1/np.sqrt(model1.alpha)*(latent_images - (1 - model1.alpha)/np.sqrt(1 - model1.alpha**float(len(time_steps_))) * generated_gaussian) + np.sqrt(1 - model1.alpha) * generated_gaussian
        for index in reversed(range(1, len(time_steps_) - 1)) :
            generated_gaussian= model1.diffusion_module(varied_tensor, time_steps_vector[index], text_embeddings)
            varied_tensor = 1/np.sqrt(model1.alpha)*(varied_tensor - (1 - model1.alpha)/np.sqrt(1 - model1.alpha**float(index)) * generated_gaussian) + np.sqrt(1 - model1.alpha) * generated_gaussian
        generated_images = model2(varied_tensor)
        final_image = generated_images[-1]
        postprocedure(final_image, path, signature)
    except :
        print('ERROR OCCURED')




def main_stage1():
    print('''
        -----------------------------
        ---Stage 1 Is Initialized-----  
        -----------------------------

    ''')
    configuration()
    epochs = 10000
    alpha = 0.828
    time_embedding_dim = 512
    csv_path = 'descriptions.csv'
    images_path = './images'

    strategy = tf.distribute.MirroredStrategy()
    print(f'Number of available GPUs: {strategy.num_replicas_in_sync}')


    def max_len(vectors) :
        length = max(len(vec) for vec in vectors)
        return length

    with strategy.scope():
        dataset, vocab_size, magnitude = load_dataset(csv_path, images_path, GLOBAL_BATCH_SIZE, height, width)
        gross_magnitude = max_len(magnitude)

        text2image_model = Text2ImageDiffusionModel(vocab_size, BATCH_SIZE, width, height, channel, alpha)
        optimizer = Adam(learning_rate=0.001)
        loss_fn = MeanSquaredError(reduction=tf.keras.losses.Reduction.NONE)

        text2image_model.compile(optimizer=optimizer, loss=loss_fn)

    log_file_path = './log/UnetSD_Latent.log'
    save_interval = 50




    @tf.function
    def train_step(batch):
        image_inputs, text_inputs = batch[0], batch[1]
        time_steps = tf.range(0, time_embedding_dim, dtype=tf.float32)
        print(text_inputs.shape, image_inputs.shape, time_steps.shape)
        output, predictions, targets = text2image_model(text_inputs, image_inputs, time_steps)
        counter, total_loss = 0, 0
        for index in reversed(range(len(targets))) :
            solo_loss = text2image_model.train_step(optimizer, targets[index], predictions[index], loss_fn)
            counter += 1
            total_loss += solo_loss
        loss = total_loss / counter
        return output, loss


    @tf.function
    def distributed_training(inputs) :
        output, per_replicas_losses = strategy.run(train_step, args=(inputs,))
        per_replicas_losses = strategy.reduce(tf.distribute.ReduceOp.SUM, per_replicas_losses, axis=None)
        return per_replicas_losses, output



    def insurance(model1, x1) :
        with open("./temporary_checkpoints/last_latent_vector.pkl", "wb") as in_f :
            pickle.dump(x1, in_f)
        model1.save_weights(f'./temporary_checkpoints/InsuranceModel')
        converter = tf.lite.TFLiteConverter.from_keras_model(model1)
        tflite_model = converter.convert()

        with open(f'./temporary_checkpoints/InsuranceModel.tflite', 'wb') as f:
            f.write(tflite_model)




    for epoch in range(epochs):
        sparse_datum = strategy.experimental_distribute_dataset(dataset)
        iterator = iter(sparse_datum)

        num, total_losses = 0, 0
        for num_, batch in enumerate(iterator):
            per_loss, _ = distributed_training(batch)
            num += 1
            total_losses += per_loss
            print(f'per_batch_loss:{per_loss} epoch:{epoch} batch_index:{num_+1}')
        insurance(text2image_model, gross_magnitude)
        train_loss = total_losses / num

        print(f'Epoch {epoch + 1}/{epochs + 1}, Loss: {train_loss.numpy()}')
        with open(log_file_path, 'a') as log_file:
            log_file.write(f"Epoch {epoch + 1}, Batch Losses: {train_loss.numpy()}\n")

        if (epoch + 1) % save_interval == 0:
            text2image_model.save_weights(f'models/UnetSD{epoch + 1}')

            converter = tf.lite.TFLiteConverter.from_keras_model(text2image_model)
            tflite_model = converter.convert()

            with open(f'models/UnetSD{epoch + 1}.tflite', 'wb') as f:
                f.write(tflite_model)
    print('''
        -----------------------------
        ---Stage 1 Is Terminated-----  
        -----------------------------

    ''')
    return text2image_model, gross_magnitude






def main_stage2(datum, model1, magnitude) :
    print('''
        -----------------------------
        ---Stage 2 Is Initialized-----  
        -----------------------------

    ''')
    coversion_log_path = './log/ImgDecoder.log'
    strategy = tf.distribute.MirroredStrategy()
    time_embedding_dim = 512
    epochs_stage2 = 1000
    save_path = './samples'
    save_interval = 50


    with strategy.scope() :
        image_decoder = ImageDecoder(output_channels=3)
        optimizer = Adam(learning_rate=0.001)
        loss_fn = MeanSquaredError(reduction=tf.keras.losses.Reduction.NONE)
        image_decoder.compile(optimizer=optimizer, loss=loss_fn)

    print(f'Number of available GPUs: {strategy.num_replicas_in_sync}')



    def compute_loss_stage2(labels, predictions, loss_fn, l2_reg_coeff=0.01):
        per_example_loss = loss_fn(labels[..., None], predictions[..., None])
        loss = tf.nn.compute_average_loss(per_example_loss)

        
        l2_loss = tf.add_n([tf.nn.l2_loss(v) for v in image_decoder.trainable_variables])
        regularization_loss = l2_reg_coeff * l2_loss

        total_loss = loss + regularization_loss

        
        total_loss /= tf.cast(tf.reduce_prod(tf.shape(labels)[:]), tf.float32)

        return total_loss


    @tf.function
    def train_step_stage2(batch) :
        with tf.GradientTape() as tape :
            real_image, latent_image = batch[0], batch[1]
            print(real_image.shape, latent_image.shape)
            predicted_image = image_decoder(latent_image)
            scaled_loss = compute_loss_stage2(real_image, predicted_image, loss_fn)
        gradients = tape.gradient(scaled_loss, image_decoder.trainable_variables)
        optimizer.apply_gradients(zip(gradients, image_decoder.trainable_variables))
        return scaled_loss  


    @tf.function
    def distributed_train_stage2(datum) :
        per_replicas_losses = strategy.run(train_step_stage2, args=(datum,))
        per_replicas_losses = strategy.reduce(tf.distribute.ReduceOp.SUM, per_replicas_losses, axis=None)
        return per_replicas_losses




    for epoch in epochs_stage2 :
        individual_targets = []
        individual_outputs = []

        for batch_targets, batch_output in datum :
            individual_targets.extend(tf.unstack(batch_targets))
            individual_outputs.extend(tf.unstack(batch_output))
        
        targets_tensor = tf.stack(individual_targets)
        outputs_tensor = tf.stack(individual_outputs)

            
        targets_dataset = tf.data.Dataset.from_tensor_slices(targets_tensor)
        outputs_dataset = tf.data.Dataset.from_tensor_slices(outputs_tensor)

            
        tensorized_data = tf.data.Dataset.zip((targets_dataset, outputs_dataset)).shuffle(buffer_size=max(len(datum), 512), reshuffle_each_iteration=True).batch(GLOBAL_BATCH_SIZE)
        sparse_tensorized_data = strategy.experimental_distribute_dataset(tensorized_data)
        iterator = iter(sparse_tensorized_data)

        num, total_losses = 0, 0
        for num_, batch in enumerate(iterator):
            per_loss = distributed_train_stage2(batch)
            num += 1
            total_losses += per_loss
            print(f'per_batch_loss:{per_loss} epoch:{epoch + 1} batch_index:{num_+1}')
        train_loss = total_losses / num


        print(f'Epoch {epoch + 1}/{epochs_stage2}, Loss: {train_loss.numpy()}')
        with open(coversion_log_path, 'a') as log_file:
            log_file.write(f"Epoch {epoch + 1}, Batch Losses: {train_loss.numpy()}\n")

        if (epoch + 1) % save_interval == 0:
            sentence = "city parks with workshops and outdoor classrooms"
            generate_image_from_text(sentence, model1, image_decoder, width, height, time_embedding_dim, save_path,
                                     magnitude, epoch + 1)
            image_decoder.save_weights(f'models/ImgDecoder{epoch + 1}')

            converter = tf.lite.TFLiteConverter.from_keras_model(image_decoder)
            tflite_model = converter.convert()

            with open(f'models/ImgDecoder{epoch + 1}.tflite', 'wb') as f:
                f.write(tflite_model)
    print('''
        -----------------------------
        ---Stage 2 Is Terminated-----  
        -----------------------------

    ''')




def main(mode="restart"):
    time_steps = 512


    def simulation(intermediates, raw_datum, magnitude, time_steps) :
        iterator = iter(raw_datum)
        intermediates_list = []
        for num_, batch in enumerate(iterator):
            try :
                images, line = batch[0], batch[1]
                inputs = [tokenizer.word_index[i] for i in line.split(" ")]
                inputs = tf.keras.preprocessing.sequence.pad_sequences([inputs], maxlen=magnitude, padding="post")
                inputs = tf.convert_to_tensor(inputs)
                initial_image = tf.random.normal(shape=[1, width, height, 3])  
                initial_image = tf.clip_by_value(initial_image, -1, 1)
                time_steps_ = tf.range(0, time_steps, dtype=tf.float32)
                text_embeddings = intermediates.text_encoder(inputs)
                time_steps_vector = intermediates.time_embedding(time_steps_)
                latent_images = intermediates.image_encoder(initial_image)                    
                generated_gaussian = intermediates.diffusion_module(latent_images, time_steps_vector[-1], text_embeddings)
                varied_tensor = 1/np.sqrt(intermediates.alpha)*(latent_images - (1 - intermediates.alpha)/np.sqrt(1 - intermediates.alpha**float(len(time_steps_))) * generated_gaussian) + np.sqrt(1 - intermediates.alpha) * generated_gaussian
                for index in reversed(range(1, len(time_steps_) - 1)) :
                    generated_gaussian= intermediates.diffusion_module(varied_tensor, time_steps_vector[index], text_embeddings)
                    varied_tensor = 1/np.sqrt(intermediates.alpha)*(varied_tensor - (1 - intermediates.alpha)/np.sqrt(1 - intermediates.alpha**float(index)) * generated_gaussian) + np.sqrt(1 - intermediates.alpha) * generated_gaussian
                intermediates_list.append((images, varied_tensor))
                print(f'--------No.{num_ + 1}generation successful !--------')
            except :
                print(f'--------No.{num_ + 1}generation failed(All Atempts Had Tried !)--------')
                continue
        return intermediates_list



    def load_state():
        try:
            images_path_2 = './images'
            csv_path_2 = 'descriptions.csv'
            alpha = 0.828
            original_datum, vocab_size, _ = load_dataset(csv_path_2, images_path_2, GLOBAL_BATCH_SIZE, height, width)
            model_ = Text2ImageDiffusionModel(vocab_size, BATCH_SIZE, width, height, channel, alpha)
            model_.load_weights('./temporary_checkpoints/InsuranceModel')
            with open("./temporary_checkpoints/last_latent_vector.pkl", "rb") as f:
                x1 = pickle.load(f)
            latent_datum = simulation(model_, original_datum, x1, time_steps)
            return latent_datum, model_, x1                        
        except FileNotFoundError:
            return None



    if mode == 'restart':
        model, magnitude = main_stage1()
        images_path_3 = './images'
        csv_path_3 = 'descriptions.csv'
        original_datum, _, _ = load_dataset(csv_path_3, images_path_3, GLOBAL_BATCH_SIZE, height, width)
        subsequent_datum = simulation(model, original_datum, len(magnitude), time_steps)
        main_stage2(subsequent_datum, model, magnitude)
    elif mode == 'recover':
        subsequent_datum, model, magnitude = load_state()
        main_stage2(subsequent_datum, model, magnitude)





if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the main stages of the program.")
    parser.add_argument("mode", type=str, choices=["restart", "recover"],
                        help="Mode to run the program. 'train' for training mode and 'recover' for recovery mode.")
    args = parser.parse_args()
    main(args.mode)