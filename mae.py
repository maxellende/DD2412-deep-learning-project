import numpy as np
from functools import partial
import jax
import flax.linen as nn
import jax.numpy as jnp
from embeddings import PatchEmbedding, position_embedding
from vision_transformer import Block, Mlp
from utils import Identity
import optax

class MAEEncoder(nn.Module):
    img_size: int = 224
    patch_size: int = 16
    nb_channels: int = 3
    embed_dim: int = 1024
    encoder_depth: int = 24
    encoder_num_heads: int = 16
    mlp_ratio: float = 4.
    masking_func: str = "random"
    
    def setup(self):
        
        self.patch_embed = PatchEmbedding(
            img_size=self.img_size,
            patch_size=self.patch_size,
            embedding_dim=self.embed_dim,
            nb_channels=self.nb_channels)
        
        nb_patches = self.patch_embed.nb_patches
        assert nb_patches == (self.img_size//self.patch_size)**2
        
        self.encoder_block_norm_layer = nn.LayerNorm()
        
        self.cls_token = jnp.zeros((1, 1, self.embed_dim))
        pos_embed = position_embedding(nb_patches, self.embed_dim, cls_token=True)
        
        self.position_embedding = jnp.array(pos_embed)
        self.encoder_blocks = [
            Block(
                self.embed_dim,
                self.encoder_num_heads,
                self.mlp_ratio,
                qkv_bias=True,
                norm_layer=self.encoder_block_norm_layer
                )
            for i in range(self.encoder_depth)
            ]
        self.encoder_norm_layer = nn.LayerNorm()
        
        if self.masking_func == "random":
            self.masking = random_masking
        elif self.masking_func == "grid":
            self.masking = grid_masking
        else:
            raise ValueError("Wrong masking function: should be either random or grid.")
    
    def __call__(self, x, mask_ratio, train, key):
        """ Encoder part of the MAE, that contains the creation of the patches + random masking
        """
        x = self.patch_embed(x)
        
        x += self.position_embedding[:, 1:, :]
        
        keys = jax.random.split(key, x.shape[0])
        #x, mask, ids_restore = random_masking(x, mask_ratio, keys)
        #x, mask, ids_restore = grid_masking(x, mask_ratio, keys)
        x, mask, ids_restore = self.masking(x, mask_ratio, keys)
        
        cls_token = self.cls_token + self.position_embedding[:, :1, :]
        cls_tokens = jnp.tile(cls_token, (x.shape[0], 1, 1))
        x = jnp.concatenate([cls_tokens, x], axis=1)
        
        # apply the transformer
        for l in self.encoder_blocks:
            x = l(x, train)
        x = self.encoder_norm_layer(x)
        
        return x, mask, ids_restore
    
    def _unbind(self):
        variables = self.variables
        module = self.clone()
        return module, variables

class MAEDecoder(nn.Module):
    nb_patches: int
    patch_size: int = 16
    nb_channels : int = 3
    decoder_embed_dim : int = 512
    decoder_depth : int = 8
    decoder_num_heads : int = 16
    mlp_ratio : float = 4.
    
    def setup(self):
        self.decoder_block_norm_layer = nn.LayerNorm()
        decoder_pos_embed = position_embedding(self.nb_patches, self.decoder_embed_dim, cls_token=True)
        
        self.decoder_embedding = nn.Dense(self.decoder_embed_dim, use_bias=True)
        self.mask_token = jnp.zeros((1, 1, self.decoder_embed_dim))
        self.decoder_position_embedding = jnp.array(decoder_pos_embed)
        self.decoder_blocks = [
            Block(
                self.decoder_embed_dim,
                self.decoder_num_heads,
                self.mlp_ratio,
                qkv_bias=True,
                norm_layer=self.decoder_block_norm_layer
                )
            for i in range(self.decoder_depth)
            ]
        self.decoder_norm_layer = nn.LayerNorm()
        self.decoder_prediction = nn.Dense(self.patch_size**2 * self.nb_channels, use_bias=True)
    
    def __call__(self, x, ids_restore, train):
        """ Decoder part of the MAE
        """
        x = self.decoder_embedding(x)

        # append mask tokens to sequence
        mask_tokens = jnp.tile(self.mask_token, (x.shape[0], ids_restore.shape[1] + 1 - x.shape[1], 1))
        x_ = jnp.concatenate([x[:, 1:, :], mask_tokens], axis=1)  # no cls token
        reshaped_ids = jnp.tile(ids_restore.reshape(ids_restore.shape[0], ids_restore.shape[1], -1), (1, 1, x.shape[2]))
        x_ = jnp.take_along_axis(x_, reshaped_ids, axis=1)  # unshuffle
        x = jnp.concatenate([x[:, :1, :], x_], axis=1)  # append cls token

        # add pos embed
        x += self.decoder_position_embedding

        # apply Transformer blocks
        for l in self.decoder_blocks:
            x = l(x, train)
        x = self.decoder_norm_layer(x)

        # predictor projection
        x = self.decoder_prediction(x)

        # remove cls token
        x = x[:, 1:, :]

        return x
    
    def _unbind(self):
        variables = self.variables
        module = self.clone()
        return module, variables

class MAEViT(nn.Module):
    img_size: int = 224
    patch_size: int = 16
    nb_channels: int = 3
    embed_dim: int = 1024
    encoder_depth: int = 24
    encoder_num_heads: int = 16
    decoder_embed_dim: int = 512
    decoder_depth: int = 8
    decoder_num_heads: int = 16
    mlp_ratio: float = 4.
    norm_pix_loss: bool = False
    masking_func: str = "random"
    
    def setup(self):
        """ Setup the layers for the MAE and compute the positional embedding for the patches
        """
        self.nb_patches = (self.img_size//self.patch_size)**2
        
        # ENCODER
        self.encoder = MAEEncoder(
            img_size=self.img_size,
            patch_size=self.patch_size,
            nb_channels=self.nb_channels,
            embed_dim=self.embed_dim,
            encoder_depth=self.encoder_depth,
            encoder_num_heads=self.encoder_num_heads,
            mlp_ratio=self.mlp_ratio,
            masking_func=self.masking_func
        )
        
        # DECODER
        self.decoder = MAEDecoder(
            nb_patches=self.nb_patches,
            patch_size=self.patch_size,
            nb_channels=self.nb_channels,
            decoder_embed_dim=self.decoder_embed_dim,
            decoder_depth=self.decoder_depth,
            decoder_num_heads=self.decoder_num_heads,
            mlp_ratio=self.mlp_ratio)
        
    def __call__(self, x, train, key, mask_ratio):
        """ Run the forward path of the MAE
        """
        #t1 = time.time()
        z, mask, ids_restore = self.encoder(x=x, mask_ratio=mask_ratio, train=train, key=key)
        #print("(MAE forward) Time to compute encoder forward: {:.4f}s".format(time.time()-t1))
        
        #t1 = time.time()
        y = self.decoder(x=z, ids_restore=ids_restore, train=train)  # [N, L, p*p*3]
        #print("(MAE forward) Time to compute decoder forward: {:.4f}s".format(time.time()-t1))
        
        return y, mask
    
    def _unbind(self):
        variables = self.variables
        module = self.clone()
        return module, variables
    
class MAEClassifier(nn.Module):
    num_classes: int
    backbone: nn.Module
    use_fc_norm: bool = True
    global_pool: bool = False
    
    def setup(self):
        self.fc_norm = nn.LayerNorm() if self.use_fc_norm else Identity()
        #self.head = nn.Dense(self.num_classes, name="head", kernel_init=nn.zeros) if self.num_classes > 0 else Identity()
        self.head = Mlp(
            in_features=128,
            hidden_features=128,
            out_features=self.num_classes,
            act_layer=nn.gelu,
            bias=True,
            drop=0.)
    
    def __call__(self, x, mask_ratio, train, key):
        z, mask, ids_restore = self.backbone(x, mask_ratio, train, key)
        
        if self.global_pool:
            z = jnp.mean(z[:, 1:, :], axis=1)  # global pool without cls token
            z = self.fc_norm(z)
        else:
            z = self.fc_norm(z)
            z = z[:, 0]
        
        output = self.head(z, train=train)
        
        return output
    
    def _unbind(self):
        variables = self.variables
        module = self.clone()
        return module, variables

@partial(jax.jit, static_argnames="p")
@partial(jax.vmap, in_axes=(0, None), out_axes=0)
def create_patches(x, p):
    """ Given an image, create a list of patches for that image from left to right and top to bottom
    """
    #p = self.patch_size
    assert x.shape[1] == x.shape[2] and x.shape[1] % p == 0
    h = w = x.shape[1] // p
    x_patches = x.reshape((3, h, p, w, p))
    x_patches = jnp.einsum("chpwq->hwpqc", x_patches)
    x_patches = x_patches.reshape((h * w, p**2 * 3))
    
    return x_patches

@partial(jax.jit, static_argnames="p")
@partial(jax.vmap, in_axes=(0, None), out_axes=0)
def recreate_images(x, p):
    """ Given a list of patches, recreate the corresponding image
    x: (L, p**2 * 3)
    imgs: (3, H, W)
    """
    #p = self.patch_size
    h = w = int(x.shape[0]**.5)
    assert h * w == x.shape[0]
    
    x = x.reshape((h, w, p, p, 3))
    x = jnp.einsum('hwpqc->chpwq', x)
    imgs = x.reshape((3, h * p, h * p))
    
    return imgs

@partial(jax.jit, static_argnames="mask_ratio")
@partial(jax.vmap, in_axes=(0, None, 0), out_axes=0)
def random_masking(x, mask_ratio, key):
    """ Perform a random masking on the embeddings of the patches
    """
    L, D = x.shape
    keep = int(L*(1-mask_ratio))
    
    # shuffle indices
    noise = jax.random.uniform(key, shape=(L,))
    ids_shuffle = jnp.argsort(noise)
    ids_restore = jnp.argsort(ids_shuffle)
    
    ids_keep = ids_shuffle[:keep]
    x_masked = x[ids_keep, :]
    
    # generate the binary mask: 0 is keep, 1 is remove
    mask = jnp.ones(L)
    mask = mask.at[:keep].set(0.)
    mask = mask[ids_restore]
    
    return x_masked, mask, ids_restore

@partial(jax.jit, static_argnames="mask_ratio")
@partial(jax.vmap, in_axes=(0, None, 0), out_axes=0)
def grid_masking(x, mask_ratio, key):
    """ Perform a random masking on the embeddings of the patches
    """
    assert mask_ratio == .5 or mask_ratio == .75
    L, D = x.shape
    
    nb_patches = int(L**(1/2)) # number of patches on one line of the image
    keep = int(1/(1-mask_ratio))//2
    
    # shuffle indices
    ids_restore = jnp.arange(0, L)
    even_rows = [i for i in range(0, nb_patches, 2)]
    ids_keep = jnp.array([
        [i for i in range(row*nb_patches, (row+1) * nb_patches, keep)] for row in even_rows
        ]).flatten()
    
    x_masked = x[ids_keep, :]
    
    # generate the binary mask: 0 is keep, 1 is remove
    mask = jnp.ones(L)
    mask = mask.at[ids_keep].set(0.)
    mask = mask[ids_restore]
    
    return x_masked, mask, ids_restore

def mae_loss(model, params, x, train, mask_ratio, key):
    """ Compute the MSE loss between the original image and the reconstructed image only on the visible patches
    """
    key, dropout_apply_rng, drop_path_apply_rng, masked_rng = jax.random.split(key, 4)
    #t1 = time.time()
    target = create_patches(x, model.patch_size)
    #print("(Loss func) Time spent to create the patches: {:.4f}s".format(time.time()-t1))

    #t1 = time.time()
    y, mask = model.apply(
        {'params': params},
        x=x,
        train=train,
        mask_ratio=mask_ratio,
        key=masked_rng,
        rngs={"dropout": dropout_apply_rng, "drop_path": drop_path_apply_rng}
        )
    #print("(Loss func) Time spent to forward model: {:.4f}s".format(time.time()-t1))
    
    loss = jnp.mean(jnp.square(y - target), axis=-1) # [N, L], mean loss per patch
    loss = jnp.sum(loss * mask) / jnp.sum(mask)  # mean loss on removed patches
    return loss, key

def mae_norm_pix_loss(model, params, x, train, mask_ratio, key):
    """ Compute the MSE loss on the visible patches with a normalized value for all the pixels of the original image
    """
    key, dropout_apply_rng, drop_path_apply_rng, masked_rng = jax.random.split(key, 4)
    target = create_patches(x, model.patch_size)
    mean = jnp.mean(target, axis=-1, keepdims=True)
    var = jnp.var(target, axis=-1, keepdims=True)
    target = (target - mean) / (var + 1.e-6)**.5

    y, mask = model.apply(
        {'params': params},
        x=x,
        train=train,
        mask_ratio=mask_ratio,
        key=masked_rng,
        rngs={"dropout": dropout_apply_rng, "drop_path": drop_path_apply_rng}
        )
    loss = jnp.mean(jnp.square(y - target), axis=-1) # [N, L], mean loss per patch

    loss = jnp.sum((loss * mask)) / jnp.sum(mask)  # mean loss on removed patches
    return loss, key

@partial(jax.jit, static_argnames=["model", "mask_ratio", "train"])
def mae_cls_loss(model, params, x, train, mask_ratio, key):
    imgs, labels = x
    
    key, dropout_apply_rng, drop_path_apply_rng, masked_rng = jax.random.split(key, 4)
    logits = model.apply(
        {"params": params},
        x=imgs,
        mask_ratio=mask_ratio,
        train=train,
        key=masked_rng,
        rngs={"dropout": dropout_apply_rng, "drop_path": drop_path_apply_rng}
        )
    
    loss = optax.softmax_cross_entropy(logits, labels).mean()
    preds = jax.nn.one_hot(logits.argmax(axis=-1), labels.shape[1])
    acc = (preds == labels).mean()
    
    return loss, acc, key

@partial(jax.jit, static_argnames=["model", "mask_ratio", "train"])
def mae_self_supervised_contrastive_loss(model, params, x, train, mask_ratio, temperature, key):
    imgs, labels = x
    imgs = jnp.concatenate([imgs[0], imgs[1]], axis=0)
    
    key, dropout_apply_rng, drop_path_apply_rng, masked_rng = jax.random.split(key, 4)
    features, mask = model.apply(
        {"params": params},
        x=imgs,
        mask_ratio=mask_ratio,
        train=train,
        key=masked_rng,
        rngs={"dropout": dropout_apply_rng, "drop_path": drop_path_apply_rng}
        )
    
    mask = jnp.expand_dims(mask, -1)
    mask = jnp.tile(mask, (1, 1, model.patch_size**2 * 3))  # (N, H*W, p*p*3)
    features = jnp.reshape(features * (1 - mask), (features.shape[0], -1))
    
    # Calculate cosine similarity between all images
    cos_sim = optax.cosine_similarity(features[:,None,:], features[None,:,:])
    cos_sim /= temperature
    
    # Masking cosine similarities to itself
    diag_range = jnp.arange(features.shape[0], dtype=jnp.int32)
    cos_sim = cos_sim.at[diag_range, diag_range].set(-9e15)
    
    # Find positive example -> batch_size//2 away from the original example
    shifted_diag = jnp.roll(diag_range, imgs.shape[0]//2)
    pos_logits = cos_sim[diag_range, shifted_diag]
    
    # InfoNCE loss
    nll = - pos_logits + nn.logsumexp(cos_sim, axis=-1)
    nll = nll.mean()
    
    return nll, key

@partial(jax.jit, static_argnames=["model", "mask_ratio", "train"])
def mae_supervised_contrastive_loss(model, params, x, train, mask_ratio, key):
    imgs, labels = x
    
    key, dropout_apply_rng, drop_path_apply_rng, masked_rng = jax.random.split(key, 4)
    features, mask = model.apply({"params": params}, x=imgs, mask_ratio=mask_ratio, train=train, key=masked_rng, rngs={"dropout": dropout_apply_rng, "drop_path": drop_path_apply_rng})
    
    features = features * (1 - mask)
    
    return None