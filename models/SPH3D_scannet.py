import tensorflow as tf
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)
sys.path.append(os.path.join(BASE_DIR, '../utils'))
import sph3gcn_util as s3g_util


def normalize_xyz(points):
    min_xyz = tf.reduce_min(points, axis=1, keepdims=True)
    max_xyz = tf.reduce_max(points, axis=1, keepdims=True)
    center = (max_xyz+min_xyz)/2
    xy = points[:,:,0:2] - center[:,:,0:2]
    z = points[:, :, 2:] # keep the range of height dimension as [0, xxx]
    points = tf.concat((xy, z), axis=2)

    return points


def _separable_conv3d_block(net, list_channels, bin_size, nn_index, nn_count, filt_idx,
                            name, depth_multiplier=None, weight_decay=None, reuse=None,
                            with_bn=True, with_bias=True, is_training=None):
    for l, num_out_channels in enumerate(list_channels):
        scope = name + '_' + str(l+1) # number from 1, not 0
        net = s3g_util.separable_conv3d(net, num_out_channels, bin_size,
                                        depth_multiplier[l], scope, nn_index,
                                        nn_count, filt_idx, weight_decay=weight_decay,
                                        with_bn=with_bn, with_bias=with_bias,
                                        reuse=reuse, is_training=is_training)
    return net


def get_model(points, is_training, config=None):
    end_points = {}
    xyz = points[:, :, 0:3]
    if config.normalize:
        norm_xyz = normalize_xyz(xyz)

    reuse = None
    net = tf.concat((norm_xyz,points[:,:,6:]),axis=2)
    net = s3g_util.pointwise_conv3d(net, config.mlp, 'mlp1',
                                    weight_decay=config.weight_decay,
                                    with_bn=config.with_bn, with_bias=config.with_bias,
                                    reuse=reuse, is_training=is_training)

    xyz_layers = []
    encoder = []
    xyz_layers.append(xyz)
    # ===============================================Encoder================================================
    for l in range(len(config.radius)):
        intra_idx, intra_cnt, \
        intra_dst, indices = s3g_util.build_graph(xyz, config.radius[l], config.nn_uplimit[l],
                                                  config.num_sample[l], sample_method=config.sample)
        filt_idx = s3g_util.spherical_kernel(xyz, xyz, intra_idx, intra_cnt,
                                             intra_dst, config.radius[l],
                                             kernel=config.kernel)
        net = _separable_conv3d_block(net, config.channels[l], config.binSize, intra_idx, intra_cnt,
                                      filt_idx, 'conv'+str(l+1), config.multiplier[l], reuse=reuse,
                                      weight_decay=config.weight_decay, with_bn=config.with_bn,
                                      with_bias=config.with_bias, is_training=is_training)

        encoder.append(net)
        if config.num_sample[l]>1:
            # ==================================gather_nd====================================
            xyz = tf.gather_nd(xyz, indices)
            xyz_layers.append(xyz)
            inter_idx = tf.gather_nd(intra_idx, indices)
            inter_cnt = tf.gather_nd(intra_cnt, indices)
            inter_dst = tf.gather_nd(intra_dst, indices)
            # =====================================END=======================================

            net = s3g_util.pool3d(net, inter_idx, inter_cnt,
                                  method=config.pool_method, scope='pool'+str(l+1))
    # ===============================================The End================================================

    config.radius.reverse()
    config.nn_uplimit.reverse()
    config.channels.reverse()
    config.multiplier.reverse()
    xyz_layers.reverse()
    encoder.reverse()
    # ===============================================Decoder================================================
    for l in range(len(config.radius)):
        xyz = xyz_layers[l]
        xyz_unpool = xyz_layers[l+1]

        intra_idx, intra_cnt, intra_dst, \
        inter_idx, inter_cnt, inter_dst = s3g_util.build_graph_deconv(xyz, xyz_unpool,
                                                                      config.radius[l],
                                                                      config.nn_uplimit[l])
        filt_idx = s3g_util.spherical_kernel(xyz, xyz, intra_idx, intra_cnt,
                                             intra_dst, config.radius[l], kernel=config.kernel)
        net = _separable_conv3d_block(net, config.channels[l], config.binSize, intra_idx, intra_cnt,
                                      filt_idx, 'deconv'+str(l+1), config.multiplier[l], reuse=reuse,
                                      weight_decay=config.weight_decay, with_bn=config.with_bn,
                                      with_bias=config.with_bias, is_training=is_training)

        net = s3g_util.unpool3d(net, inter_idx, inter_cnt, inter_dst,
                                method=config.unpool_method, scope='unpool'+str(l+1))
        net = tf.concat((net,encoder[l]),axis=2)
    # ===============================================The End================================================
    end_points['feats'] = net

    # point-wise classifier
    net = s3g_util.pointwise_conv3d(net, config.num_cls, scope='logits', with_bn=False, with_bias=config.with_bias,
                                   activation_fn=None, is_training=is_training)

    return net, end_points


def get_loss(pred, label, end_points, inner_label):
    """ pred: B*NUM_CLASSES,
        label: B, """
    loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=pred, labels=label)
    classify_loss = 0.0
    bsize = pred.get_shape()[0].value
    for b in range(bsize):
        inIdx = tf.where(inner_label[b,:]>0)
        item_loss = loss[b,:]
        item_inner_loss = tf.gather_nd(item_loss,inIdx)
        classify_loss += tf.cond(tf.equal(tf.size(item_inner_loss), 0), lambda: 0.0, \
                                 lambda:tf.reduce_mean(item_inner_loss))

    tf.summary.scalar('classify loss', classify_loss)
    tf.add_to_collection('losses', classify_loss)
    return classify_loss