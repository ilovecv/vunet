import PIL.Image
from multiprocessing.pool import ThreadPool
import numpy as np
import pickle
import os
import cv2
import math
from numpy.random import RandomState


def get_orientation(joints, jo):
    return (min(joints[jo.index("lhip"),0],joints[jo.index("lshoulder"),0]) <
            max(joints[jo.index("rhip"),0],joints[jo.index("rshoulder"),0]))


def flip(j,x,c):
    x = cv2.flip(x, 1)
    c = cv2.flip(c, 1)
    width = x.shape[1]
    j[:,0] = width - 1 - j[:,0]
    return j,x,c


def register(xs,cs,srcs,targets,ys,jo):
    #print("Registering")
    bs = xs.shape[0]

    xx = list()
    cc = list()
    for i in range(bs):
        x = xs[i]
        c = cs[i]
        src = srcs[i]
        target = targets[i]

        valid_mask = (src >= 0.0) & (target >= 0.0)
        valid_mask = np.all(valid_mask, axis = 1)

        valid_src = src[valid_mask]
        valid_target = target[valid_mask]

        fall_back = False

        if np.sum(valid_mask) >=  4:
            # figure out orientation and flip if necessary to find restricted
            # affine transforms
            src_orient = get_orientation(src, jo)
            dst_orient = get_orientation(target, jo)
            if src_orient != dst_orient:
                valid_src, x, c = flip(valid_src, x, c)

            affine = True
            if affine:
                M = cv2.estimateRigidTransform(valid_src, valid_target, fullAffine = False)
                if M is None:
                    fall_back = True
                else:
                    warped_x = cv2.warpAffine(x, M, x.shape[:2], borderMode = cv2.BORDER_REPLICATE)
                    xx.append(warped_x)

                    warped_c = cv2.warpAffine(c, M, x.shape[:2], borderMode = cv2.BORDER_REPLICATE)
                    cc.append(warped_c)
            else:
                M, mask = cv2.findHomography(valid_src, valid_target, cv2.RANSAC,5.0)
                #M, mask = cv2.findHomography(valid_src, valid_target)

                warped_x = cv2.warpPerspective(x, M, x.shape[:2], borderMode = cv2.BORDER_REPLICATE)
                xx.append(warped_x)

                warped_c = cv2.warpPerspective(c, M, x.shape[:2], borderMode = cv2.BORDER_REPLICATE)
                cc.append(warped_c)
        else:
            fall_back = True

        if fall_back:
            xx.append(x)
            cc.append(c)

    xx = np.stack(xx)
    cc = np.stack(cc)

    #plot_batch(xs,"xs.png")
    #plot_batch(xx,"xx.png")
    #plot_batch(ys,"ys.png")

    return xx,cc


class BufferedWrapper(object):
    """Fetch next batch asynchronuously to avoid bottleneck during GPU
    training."""
    def __init__(self, gen):
        self.gen = gen
        self.n = gen.n
        self.jo = gen.jo
        self.pool = ThreadPool(1)
        self._async_next()


    def _async_next(self):
        self.buffer_ = self.pool.apply_async(next, (self.gen,))


    def __next__(self):
        result = self.buffer_.get()
        self._async_next()
        return result


def load_img(path, target_size):
    """Load image. target_size is specified as (height, width, channels)
    where channels == 1 means grayscale. uint8 image returned."""
    img = PIL.Image.open(path)
    grayscale = target_size[2] == 1
    if grayscale:
        if img.mode != 'L':
            img = img.convert('L')
    else:
        if img.mode != 'RGB':
            img = img.convert('RGB')
    wh_tuple = (target_size[1], target_size[0])
    if img.size != wh_tuple:
        img = img.resize(wh_tuple, resample = PIL.Image.BILINEAR)

    x = np.asarray(img, dtype = "uint8")
    if len(x.shape) == 2:
        x = np.expand_dims(x, -1)

    return x


def save_image(X, name):
    """Save image as png."""
    fname = os.path.join(out_dir, name + ".png")
    PIL.Image.fromarray(X).save(fname)


def preprocess(x):
    """From uint8 image to [-1,1]."""
    return np.cast[np.float32](x / 127.5 - 1.0)


def preprocess_mask(x):
    """From uint8 mask to [0,1]."""
    mask = np.cast[np.float32](x / 255.0)
    if mask.shape[-1] == 3:
        mask = np.amax(mask, axis = -1, keepdims = True)
    return mask


def postprocess(x):
    """[-1,1] to uint8."""
    x = (x + 1.0) / 2.0
    x = np.clip(255 * x, 0, 255)
    x = np.cast[np.uint8](x)
    return x


def tile(X, rows, cols):
    """Tile images for display."""
    tiling = np.zeros((rows * X.shape[1], cols * X.shape[2], X.shape[3]), dtype = X.dtype)
    for i in range(rows):
        for j in range(cols):
            idx = i * cols + j
            if idx < X.shape[0]:
                img = X[idx,...]
                tiling[
                        i*X.shape[1]:(i+1)*X.shape[1],
                        j*X.shape[2]:(j+1)*X.shape[2],
                        :] = img
    return tiling


def plot_batch(X, out_path):
    """Save batch of images tiled."""
    X = postprocess(X)
    rc = math.sqrt(X.shape[0])
    rows = cols = math.ceil(rc)
    canvas = tile(X, rows, cols)
    canvas = np.squeeze(canvas)
    PIL.Image.fromarray(canvas).save(out_path)


def make_joint_img(img_shape, jo, joints):
    # three channels: left, right, center
    scale_factor = img_shape[1] / 128
    thickness = int(3 * scale_factor)
    imgs = list()
    for i in range(3):
        imgs.append(np.zeros(img_shape[:2], dtype = "uint8"))

    assert("cnose" in jo)
    # MSCOCO
    body = ["lhip", "lshoulder", "rshoulder", "rhip"]
    body_pts = np.array([[joints[jo.index(part),:] for part in body]])
    if np.min(body_pts) >= 0:
        body_pts = np.int_(body_pts)
        cv2.fillPoly(imgs[2], body_pts, 255)

    right_lines = [
            ("rankle", "rknee"),
            ("rknee", "rhip"),
            ("rhip", "rshoulder"),
            ("rshoulder", "relbow"),
            ("relbow", "rwrist")]
    for line in right_lines:
        l = [jo.index(line[0]), jo.index(line[1])]
        if np.min(joints[l]) >= 0:
            a = tuple(np.int_(joints[l[0]]))
            b = tuple(np.int_(joints[l[1]]))
            cv2.line(imgs[0], a, b, color = 255, thickness = thickness)

    left_lines = [
            ("lankle", "lknee"),
            ("lknee", "lhip"),
            ("lhip", "lshoulder"),
            ("lshoulder", "lelbow"),
            ("lelbow", "lwrist")]
    for line in left_lines:
        l = [jo.index(line[0]), jo.index(line[1])]
        if np.min(joints[l]) >= 0:
            a = tuple(np.int_(joints[l[0]]))
            b = tuple(np.int_(joints[l[1]]))
            cv2.line(imgs[1], a, b, color = 255, thickness = thickness)

    rs = joints[jo.index("rshoulder")]
    ls = joints[jo.index("lshoulder")]
    cn = joints[jo.index("cnose")]
    neck = 0.5*(rs+ls)
    a = tuple(np.int_(neck))
    b = tuple(np.int_(cn))
    if np.min(a) >= 0 and np.min(b) >= 0:
        cv2.line(imgs[0], a, b, color = 127, thickness = thickness)
        cv2.line(imgs[1], a, b, color = 127, thickness = thickness)

    cn = tuple(np.int_(cn))
    leye = tuple(np.int_(joints[jo.index("leye")]))
    reye = tuple(np.int_(joints[jo.index("reye")]))
    if np.min(reye) >= 0 and np.min(leye) >= 0 and np.min(cn) >= 0:
        cv2.line(imgs[0], cn, reye, color = 255, thickness = thickness)
        cv2.line(imgs[1], cn, leye, color = 255, thickness = thickness)

    img = np.stack(imgs, axis = -1)
    if img_shape[-1] == 1:
        img = np.mean(img, axis = -1)[:,:,None]
    return img


def make_mask_img(img_shape, jo, joints):
    scale_factor = img_shape[1] / 128
    masks = 3*[None]
    for i in range(3):
        masks[i] = np.zeros(img_shape[:2], dtype = "uint8")

    body = ["lhip", "lshoulder", "rshoulder", "rhip"]
    body_pts = np.array([[joints[jo.index(part),:] for part in body]], dtype = np.int32)
    cv2.fillPoly(masks[1], body_pts, 255)

    head = ["lshoulder", "chead", "rshoulder"]
    head_pts = np.array([[joints[jo.index(part),:] for part in head]], dtype = np.int32)
    cv2.fillPoly(masks[2], head_pts, 255)

    thickness = int(15 * scale_factor)
    lines = [[
        ("rankle", "rknee"),
        ("rknee", "rhip"),
        ("rhip", "lhip"),
        ("lhip", "lknee"),
        ("lknee", "lankle") ], [
            ("rhip", "rshoulder"),
            ("rshoulder", "relbow"),
            ("relbow", "rwrist"),
            ("rhip", "lhip"),
            ("rshoulder", "lshoulder"),
            ("lhip", "lshoulder"),
            ("lshoulder", "lelbow"),
            ("lelbow", "lwrist")], [
                ("rshoulder", "chead"),
                ("rshoulder", "lshoulder"),
                ("lshoulder", "chead")]]
    for i in range(len(lines)):
        for j in range(len(lines[i])):
            line = [jo.index(lines[i][j][0]), jo.index(lines[i][j][1])]
            a = tuple(np.int_(joints[line[0]]))
            b = tuple(np.int_(joints[line[1]]))
            cv2.line(masks[i], a, b, color = 255, thickness = thickness)

    for i in range(3):
        r = int(11 * scale_factor)
        if r % 2 == 0:
            r = r + 1
        masks[i] = cv2.GaussianBlur(masks[i], (r,r), 0)
        maxmask = np.max(masks[i])
        if maxmask > 0:
            masks[i] = masks[i] / maxmask
    mask = np.stack(masks, axis = -1)
    mask = np.uint8(255 * mask)

    return mask


class IndexFlow(object):
    """Batches from index file."""

    def __init__(
            self,
            shape,
            index_path,
            train,
            mask = True,
            fill_batches = True,
            shuffle = True,
            return_keys = ["imgs", "joints"],
            prefix = None,
            seed = 1):
        self.prng = RandomState(seed)
        self.shape = shape
        self.batch_size = self.shape[0]
        self.img_shape = self.shape[1:]
        with open(index_path, "rb") as f:
            self.index = pickle.load(f)
        self.basepath = os.path.dirname(index_path)
        self.train = train
        self.mask = mask
        self.fill_batches = fill_batches
        self.shuffle_ = False
        self.return_keys = return_keys

        self.jo = self.index["joint_order"]
        if prefix is None:
            prefix = ""
        self.indices = np.array(
                [i for i in range(len(self.index["train"]))
                    if self._filter(i)])
        # rescale joint coordinates to image shape
        h,w = self.img_shape[:2]
        wh = np.array([[[w,h]]])
        self.index["joints"] = self.index["joints"] * wh

        self.n = self.indices.shape[0]
        self.shuffle()


    def _filter(self, i):
        good = True
        good = good and (self.index["train"][i] == self.train)
        fname = self.index["imgs"][i]
        valid_fnames = [
            "03079_1.jpg",  "03079_2.jpg",  "03079_4.jpg",  "03079_7.jpg",
            "07395_1.jpg",  "07395_2.jpg",  "07395_3.jpg",  "07395_7.jpg",
            "09614_1.jpg",  "09614_2.jpg",  "09614_3.jpg",  "09614_4.jpg",
            "00038_1.jpg",  "00038_2.jpg",  "00038_3.jpg",  "00038_7.jpg",
            "01166_1.jpg",  "01166_2.jpg",  "01166_3.jpg",  "01166_4.jpg",
            "00281_1.jpg",  "00281_2.jpg",  "00281_3.jpg",  "00281_7.jpg",
            "09874_1.jpg",  "09874_3.jpg",  "09874_4.jpg",  "09874_7.jpg",
            "06909_1.jpg",  "06909_2.jpg",  "06909_3.jpg",  "06909_4.jpg",
            "07586_1.jpg",  "07586_2.jpg",  "07586_3.jpg",  "07586_4.jpg",
            "07607_1.jpg",  "07607_2.jpg",  "07607_3.jpg",  "07607_7.jpg"]
        # 10*4 = 40
        good = good and (fname in valid_fnames)
        return good


    def __next__(self):
        batch = dict()

        # get indices for batch
        batch_start, batch_end = self.batch_start, self.batch_start + self.batch_size
        batch_indices = self.indices[batch_start:batch_end]
        if self.fill_batches and batch_indices.shape[0] != self.batch_size:
            n_missing = self.batch_size - batch_indices.shape[0]
            batch_indices = np.concatenate([batch_indices, self.indices[:n_missing]], axis = 0)
            assert(batch_indices.shape[0] == self.batch_size)
        batch_indices = np.array(batch_indices)
        batch["indices"] = batch_indices

        # prepare next batch
        if batch_end >= self.n:
            self.shuffle()
        else:
            self.batch_start = batch_end

        # prepare batch data
        # load images
        batch["imgs"] = list()
        for i in batch_indices:
            fname = self.index["imgs"][i]
            traintest = "train" if self.train else "test"
            path = os.path.join(self.basepath, "..", "original", "filted_up_{}".format(traintest), fname)
            batch["imgs"].append(load_img(path, target_size = self.img_shape))
        batch["imgs"] = np.stack(batch["imgs"])
        batch["imgs"] = preprocess(batch["imgs"])

        # load joint coordinates
        batch["joints_coordinates"] = [self.index["joints"][i] for i in batch_indices]

        # generate stickmen images from coordinates
        batch["joints"] = list()
        for joints in batch["joints_coordinates"]:
            img = make_joint_img(self.img_shape, self.jo, joints)
            batch["joints"].append(img)
        batch["joints"] = np.stack(batch["joints"])
        batch["joints"] = preprocess(batch["joints"])

        if False and self.mask:
            if "masks" in self.index:
                batch_masks = list()
                for i in batch_indices:
                    fname = self.index["masks"][i]
                    path = os.path.join(self.basepath, fname)
                    batch_masks.append(load_img(path, target_size = self.img_shape))
            else:
                # generate mask based on joint coordinates
                batch_masks = list()
                for joints in batch["joints_coordinates"]:
                    mask = make_mask_img(self.img_shape, self.jo, joints)
                    batch_masks.append(mask)
            batch["masks"] = np.stack(batch_masks)
            batch["masks"] = preprocess_mask(batch["masks"])
            # apply mask to images
            batch["imgs"] = batch["imgs"] * batch["masks"]

        valid_joints = ["lhip","rhip","lshoulder","rshoulder"]
        valid_joint_indices = [self.jo.index(j) for j in valid_joints]
        invalid_joint_indices = [i for i in range(len(self.jo)) if i not in valid_joint_indices]
        for i in range(len(batch["joints_coordinates"])):
            batch["joints_coordinates"][i][invalid_joint_indices,:] = -100.0

        batch_list = [batch[k] for k in self.return_keys]
        return batch_list


    def shuffle(self):
        self.batch_start = 0
        if self.shuffle_:
            self.prng.shuffle(self.indices)


def get_batches(
        shape,
        index_path,
        train,
        mask,
        fill_batches = True,
        shuffle = True,
        return_keys = ["imgs", "joints"],
        prefix = None):
    """Buffered IndexFlow."""
    flow = IndexFlow(shape, index_path, train, mask, fill_batches, shuffle, return_keys,prefix)
    return BufferedWrapper(flow)


if __name__ == "__main__":
    import sys
    if not len(sys.argv) == 2:
        print("Useage: {} <path to index.p>".format(sys.argv[0]))
        exit(1)

    batches = get_batches(
            shape = (16, 128, 128, 3),
            index_path = sys.argv[1],
            train = True,
            mask = False,
            shuffle = True)
    X, C = next(batches)
    plot_batch(X, "unmasked.png")
    plot_batch(C, "joints.png")

    """
    batches = get_batches(
            shape = (16, 128, 128, 3),
            index_path = sys.argv[1],
            train = True,
            mask = True)
    X, C = next(batches)
    plot_batch(X, "masked.png")

    batches = get_batches(
            shape = (16, 32, 32, 3),
            index_path = sys.argv[1],
            train = True,
            mask = True)
    X, C = next(batches)
    plot_batch(X, "masked32.png")
    plot_batch(C, "joints32.png")
    """
