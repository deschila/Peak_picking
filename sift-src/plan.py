#!/usr/bin/env python
# -*- coding: utf8 -*-
#
# This code implements the SIFT algorithm
# The SIFT algorithm belongs to the University of British Columbia. It is
# protected by patent US6711293. If you are on a country where this pattent
# applies (like the USA), please check if you are allowed to use it. The
# University of British Columbia does not require a license for its use for
# non-commercial research applications.
#
#    Project: Sift implementation in Python + OpenCL
#             https://github.com/kif/sift_pyocl
#
"""

Contains a class for creating a plan, allocating arrays, compiling kernels and other things like that...
to calculate SIFT keypoints and descriptors.

This algorithm is patented: U.S. Patent 6,711,293: 
"Method and apparatus for identifying scale invariant features in an image and use of same for locating an object in an image", 
David Lowe's patent for the SIFT algorithm, March 23, 2004
"""

from __future__ import division, print_function, with_statement

__authors__ = ["Jérôme Kieffer"]
__contact__ = "jerome.kieffer@esrf.eu"
__license__ = "MIT"
__copyright__ = "European Synchrotron Radiation Facility, Grenoble, France"
__date__ = "2013-07-19"
__status__ = "beta"
__license__ = """
Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

"""
import time, math, os, logging, threading, sys
# import sys
import gc
import numpy
from .param import par
from .opencl import ocl, pyopencl

from .utils import calc_size, kernel_size  # , sizeof
logger = logging.getLogger("sift.plan")

class SiftPlan(object):
    """
    This class implements a way to calculate SIFT keypoints.
    
    
    How to calculate a set of SIFT keypoint on an image:

    siftp = sift.SiftPlan(img.shape,img.dtype,devicetype="GPU")
    kp = siftp.keypoints(img)

    kp is a nx132 array. the second dimension is composed of x,y, scale and angle as well as 128 floats describing the keypoint

    This SIFT algorithm is patented: U.S. Patent 6,711,293: 
    "Method and apparatus for identifying scale invariant features in an image and use of same for locating an object in an image", 
    """
    kernels = {"convolution":1024,  # key: name value max local workgroup size
               "preprocess": 1024,
               "algebra": 1024,
               "image":1024,
               "gaussian":1024,
               "reductions":1024,
               "orientation_cpu":1,
               "orientation_gpu":128,
               "keypoints_gpu1":(8, 4, 4),
               "keypoints_gpu2":(8, 8, 8),
               "keypoints_cpu":1,
               "memset":128, }
#               "keypoints":128}
    converter = {numpy.dtype(numpy.uint8):"u8_to_float",
                 numpy.dtype(numpy.uint16):"u16_to_float",
                 numpy.dtype(numpy.uint32):"u32_to_float",
                 numpy.dtype(numpy.int32):"s32_to_float",
                 numpy.dtype(numpy.int64):"s64_to_float",
#                    numpy.float64:"double_to_float",
                      }
#     sigmaRatio = 2.0 ** (2.0 / par.Scales)
    x=[]
    y=[]
    sigmaRatio = par.FinalSigma/par.InitSigma
    PIX_PER_KP = 1  # pre_allocate buffers for keypoints
    dtype_kp = numpy.dtype([('x', numpy.float32),
                                ('y', numpy.float32),
                                ('scale', numpy.float32),
                                ('angle', numpy.float32),
                                ('desc', (numpy.uint8, 128))
                                ])

    def __init__(self, shape=None, dtype=None, devicetype="CPU", template=None, profile=False, device=None, PIX_PER_KP=None, max_workgroup_size=None, context=None, init_sigma=None):
        """
        Contructor of the class

        @param shape: shape of the input image
        @param dtype: data type of the input image
        @param devicetype: can be 'CPU' or 'GPU'
        @param template: extract shape and dtype from an image
        @param profile: collect timing info
        @param device: 2-tuple of integers
        @param PIX_PER_KP: number of keypoint pre-allocated: 1 for 10 pixel
        @param max_workgroup_size: set to 1 under macosX on CPU
        @param context: provide an external context
        """
        if init_sigma is None:
            init_sigma = par.InitSigma
        # no test on the values, just make sure it is a float
        self._init_sigma = float(init_sigma)
        self.buffers = {}
        self.programs = {}
        if template is not None:
            self.shape = template.shape
            self.dtype = template.dtype
        else:
            self.shape = shape
            self.dtype = numpy.dtype(dtype)
        if len(self.shape) == 3:
            self.RGB = True
            self.shape = self.shape[:2]
        elif len(self.shape) == 2:
            self.RGB = False
        else:
            raise RuntimeError("Unable to process image of shape %s" % (tuple(self.shape,)))
        if PIX_PER_KP :
            self.PIX_PER_KP = int(PIX_PER_KP)
        self.profile = bool(profile)
        if max_workgroup_size:
            self.max_workgroup_size = int(max_workgroup_size)
            self.kernels = {}
            for k, v in self.__class__.kernels.items():
                if isinstance(v, int):
                    self.kernels[k] = min(v, self.max_workgroup_size)
#                else:
#                    self.kernels[k] = tuple([1 for i in v])
        else:
            self.max_workgroup_size = None
        self.events = []
        self._sem = threading.Semaphore()
        self.scales = []  # in XY order
        self.procsize = []  # same as  procsize but with dimension in (X,Y) not (slow, fast)
        self.wgsize = []
        self.kpsize = None
        self.memory = None
        self.octave_max = None
        self.red_size = None
        self._calc_scales()
        self._calc_memory()
        self.LOW_END = 0
        if context:
            self.ctx = context
            device_name = self.ctx.devices[0].name.strip()
            platform_name = self.ctx.devices[0].platform.name.strip()
            platform = ocl.get_platform(platform_name)
            device = platform.get_device(device_name)
            self.device = platform.id, device.id
        else:
            if device is None:
                self.device = ocl.select_device(type=devicetype, memory=self.memory, best=True)
                if device is None:
                    self.device = ocl.select_device(memory=self.memory, best=True)
                    logger.warning('Unable to find suitable device, selecting device: %s,%s' % self.device)
            else:
                self.device = device
            self.ctx = pyopencl.Context(devices=[pyopencl.get_platforms()[self.device[0]].get_devices()[self.device[1]]])

        if profile:
            self.queue = pyopencl.CommandQueue(self.ctx, properties=pyopencl.command_queue_properties.PROFILING_ENABLE)
        else:
            self.queue = pyopencl.CommandQueue(self.ctx)
        self._calc_workgroups()
        self._compile_kernels()
        self._allocate_buffers()
        self.debug = []
        self.cnt = numpy.empty(1, dtype=numpy.int32)
        self.devicetype = ocl.platforms[self.device[0]].devices[self.device[1]].type
        if (self.devicetype == "CPU"):
            self.USE_CPU = True
            if sys.platform == "darwin":
                logger.warning("MacOSX computer working on CPU: limiting workgroup size to 1 !")
                self.max_workgroup_size = 1
                self.kernels = {}
                for k, v in self.__class__.kernels.items():
                    if isinstance(v, int):
                        self.kernels[k] = 1
                    else:
                        self.kernels[k] = tuple([1 for i in v])
        else:
            self.USE_CPU = False

    def __del__(self):
        """
        Destructor: release all buffers
        """
        self._free_kernels()
        self._free_buffers()
        self.queue = None
        self.ctx = None
        gc.collect()

    def _calc_scales(self):
        """
        Nota scales are in XY order
        """
        shape = self.shape[-1::-1]
        self.scales = [tuple(numpy.int32(i) for i in shape)]
        min_size = 2 * par.BorderDist + 2
        while min(shape) > min_size:
            shape = tuple(numpy.int32(i // 2) for i in shape)
            self.scales.append(shape)
        self.scales.pop()
        self.octave_max = len(self.scales)

    def _calc_memory(self):
        """
        Estimates the memory footprint of all buffer to ensure it fits on the device
        """
        # Just the context + kernel takes about 75MB on the GPU
        self.memory = 75 * 2 ** 20
        size_of_float = numpy.dtype(numpy.float32).itemsize
        size_of_input = numpy.dtype(self.dtype).itemsize
        # raw images:
        size = self.shape[0] * self.shape[1]
        self.memory += size * size_of_input  # initial_image (no raw_float)
        if self.RGB:
            self.memory += 2 * size * (size_of_input)  # one of three was already counted
        nr_blur = par.Scales + 3  # 3 blurs and 2 tmp
        nr_dogs = par.Scales + 2
        self.memory += size * (nr_blur + nr_dogs) * size_of_float

        self.kpsize = int(self.shape[0] * self.shape[1] // self.PIX_PER_KP)  # Is the number of kp independant of the octave ? int64 causes problems with pyopencl
        self.memory += self.kpsize * size_of_float * 4 * 2  # those are array of float4 to register keypoints, we need two of them
        self.memory += self.kpsize * 128  # stores the descriptors: 128 unsigned chars
        self.memory += 4  # keypoint index Counter
        if self.max_workgroup_size:
            wg_float = min(self.max_workgroup_size, numpy.sqrt(self.shape[0] * self.shape[1]))
        else:
            wg_float = min(128, numpy.sqrt(self.shape[0] * self.shape[1]))
        self.red_size = 2 ** (int(math.ceil(math.log(wg_float, 2))))
        self.memory += 4 * 2 * self.red_size  # temporary storage for reduction

        ########################################################################
        # Calculate space for gaussian kernels
        ########################################################################

        if self._init_sigma > par.CurSigma:
            sigma = math.sqrt(self._init_sigma ** 2 - par.CurSigma ** 2)
            size = kernel_size(sigma, True)
            logger.debug("pre-Allocating %s float for init blur" % size)
            self.memory += size * size_of_float
        prevSigma = self._init_sigma
        for i in range(par.Scales):
            if i == 0 : 
                absSigma = self._init_sigma
                increase = self._init_sigma
            else :
                absSigma = self._init_sigma * self.sigmaRatio ** (1.0*i/(par.Scales))
                increase = prevSigma * math.sqrt(self.sigmaRatio ** (2.0/(par.Scales)) - 1.0)
            size = kernel_size(increase, True)
            logger.debug("pre-Allocating %s float for blur sigma: %s" % (size, increase))
            self.memory += size * size_of_float 
            prevSigma = absSigma

    def _allocate_buffers(self):
        """
        All buffers are allocated here
        """
        shape = self.shape
        if self.dtype != numpy.float32:
            if self.RGB:
                rgbshape = self.shape[0], self.shape[1], 3
                self.buffers["raw"] = pyopencl.array.empty(self.queue, rgbshape, dtype=self.dtype)
            else:
                self.buffers["raw"] = pyopencl.array.empty(self.queue, shape, dtype=self.dtype)
        self.buffers[ "Kp_1" ] = pyopencl.array.empty(self.queue, (self.kpsize, 4), dtype=numpy.float32)
        self.buffers[ "Kp_2" ] = pyopencl.array.empty(self.queue, (self.kpsize, 4), dtype=numpy.float32)
        self.buffers[ "descr" ] = pyopencl.array.empty(self.queue, (self.kpsize, 128), dtype=numpy.uint8)
        self.buffers["cnt" ] = pyopencl.array.empty(self.queue, 1, dtype=numpy.int32)
        #self.buffers["descriptors"] = pyopencl.array.empty(self.queue, (self.kpsize, 128), dtype=numpy.uint8)

        self.buffers["tmp"] = pyopencl.array.empty(self.queue, shape, dtype=numpy.float32)
        self.buffers["ori"] = pyopencl.array.empty(self.queue, shape, dtype=numpy.float32)
        for scale in range(par.Scales + 3):
            self.buffers[scale ] = pyopencl.array.empty(self.queue, shape, dtype=numpy.float32)
        self.buffers["DoGs" ] = pyopencl.array.empty(self.queue, (par.Scales + 2, shape[0], shape[1]), dtype=numpy.float32)
        wg_float = min(512.0, numpy.sqrt(self.shape[0] * self.shape[1]))
#        wg = 2 ** (int(math.ceil(math.log(wg_float, 2))))
        self.buffers["max_min"] = pyopencl.array.empty(self.queue, (self.red_size, 2), dtype=numpy.float32)  # temporary buffer for max/min reduction
        self.buffers["min"] = pyopencl.array.empty(self.queue, (1), dtype=numpy.float32)
        self.buffers["max"] = pyopencl.array.empty(self.queue, (1), dtype=numpy.float32)
        self.buffers["255"] = pyopencl.array.to_device(self.queue, numpy.array([255.0], dtype=numpy.float32))
        
        ########################################################################
        # Allocate space for gaussian kernels
        ########################################################################
        
        
        par.CurSigma = 0.25
        if self._init_sigma > par.CurSigma:
            sigma = math.sqrt(self._init_sigma ** 2 - par.CurSigma ** 2)
            self._init_gaussian(sigma)
        prevSigma = self._init_sigma

        for i in range(par.Scales+2):
            if i == 0 : 
                absSigma = self._init_sigma
                increase = self._init_sigma
            else :
                absSigma = self._init_sigma * self.sigmaRatio ** (1.0*i/(par.Scales))
                increase = prevSigma * math.sqrt(self.sigmaRatio ** (2.0/(par.Scales)) - 1.0)
            print (absSigma, increase, prevSigma)
            self._init_gaussian(increase)
            prevSigma = absSigma
            



    def _init_gaussian(self, sigma):
        """
        Create a buffer of the right size according to the width of the gaussian ...


        @param  sigma: width of the gaussian, the length of the function will be 8*sigma + 1

        Same calculation done on CPU
        x = numpy.arange(size) - (size - 1.0) / 2.0
        gaussian = numpy.exp(-(x / sigma) ** 2 / 2.0).astype(numpy.float32)
        gaussian /= gaussian.sum(dtype=numpy.float32)
        """
        name = "gaussian_%s" % sigma
        size = kernel_size(sigma, True)
        wg_size = 2 ** int(math.ceil(math.log(size) / math.log(2)))

        logger.debug("Allocating %s float for blur sigma: %s" % (size, sigma))
        if self.max_workgroup_size and (wg_size > self.max_workgroup_size):  # compute on CPU
            x = numpy.arange(size) - (size - 1.0) / 2.0
            gaus = numpy.exp(-(x / sigma) ** 2 / 2.0).astype(numpy.float32)
            gaus /= gaus.sum(dtype=numpy.float32)
            gaussian_gpu = pyopencl.array.to_device(self.queue, gaus)
        else:
            gaussian_gpu = pyopencl.array.empty(self.queue, size, dtype=numpy.float32)
            evt = self.programs["gaussian"].gaussian(self.queue, (wg_size,), (wg_size,),
                                                gaussian_gpu.data,  # __global     float     *data,
                                                numpy.float32(sigma),  # const        float     sigma,
                                                numpy.int32(size))  # const        int     SIZE
            if self.profile: self.events.append(("gaussian %s" % sigma, evt))
        self.buffers[name] = gaussian_gpu


    def _free_buffers(self):
        """
        free all memory allocated on the device
        """
        for buffer_name in self.buffers:
            if self.buffers[buffer_name] is not None:
                try:
                    del self.buffers[buffer_name]
                    self.buffers[buffer_name] = None
                except pyopencl.LogicError:
                    logger.error("Error while freeing buffer %s" % buffer_name)

    def _compile_kernels(self):
        """
        Call the OpenCL compiler
        """
        kernel_directory = os.path.dirname(os.path.abspath(__file__))
        if not os.path.exists(os.path.join(kernel_directory, "algebra" + ".cl")):
            while (".zip" in kernel_directory)  and (len(kernel_directory) > 4):
                kernel_directory = os.path.dirname(kernel_directory)
            kernel_directory = os.path.join(kernel_directory, "sift_kernels")
        for kernel in self.kernels:
            logger.debug("Compiling OpenCL file: %s"%kernel)
            kernel_file = os.path.join(kernel_directory, kernel + ".cl")
            kernel_src = open(kernel_file).read()
            if self.max_workgroup_size:
                if "__len__" not in dir(self.kernels[kernel]):
                    wg_size = min(self.max_workgroup_size, self.kernels[kernel])
                else:
                    wg_size = self.max_workgroup_size
            else:
                if "__len__" not in dir(self.kernels[kernel]):
                    wg_size = self.kernels[kernel]
                else:
                    wg_size = 128
            try:
                program = pyopencl.Program(self.ctx, kernel_src).build('-D WORKGROUP_SIZE=%s' % wg_size)
            except pyopencl.MemoryError as error:
                raise MemoryError(error)
            except pyopencl.RuntimeError as error:
                if kernel == "keypoints_gpu2":
                    logger.warning("Failed compiling kernel '%s' with workgroup size %s: %s: use low_end alternative", kernel, wg_size, error)
                    self.LOW_END += 1
                elif kernel == "keypoints_gpu1":
                    logger.warning("Failed compiling kernel '%s' with workgroup size %s: %s: use CPU alternative", kernel, wg_size, error)
                    self.LOW_END += 1
                else:
                    logger.error("Failed compiling kernel '%s' with workgroup size %s: %s", kernel, wg_size, error)
                    raise error
            self.programs[kernel] = program

    def _free_kernels(self):
        """
        free all kernels
        """
        self.programs = {}

    def _calc_workgroups(self):
        """
        First try to guess the best workgroup size, then calculate all global worksize

        Nota:
        The workgroup size is limited by the device
        The workgroup size is limited to the 2**n below then image size (hence changes with octaves)
        The second dimension of the wg size should be large, the first small: i.e. (1,64)
        The processing size should be a multiple of  workgroup size.
        """
        device = self.ctx.devices[0]
#        max_work_group_size = device.max_work_group_size
        max_work_item_sizes = device.max_work_item_sizes
        # we recalculate the shapes ...
        shape = self.shape
        min_size = 2 * par.BorderDist + 2
        if self.max_workgroup_size:
            self.max_workgroup_size = min(self.max_workgroup_size, max_work_item_sizes[0])
        else:
            self.max_workgroup_size = max_work_item_sizes[0]
        while min(shape) > min_size:
            wg = (min(2 ** int(math.ceil(math.log(shape[-1], 2))), self.max_workgroup_size), 1)
            self.wgsize.append(wg)
            self.procsize.append(calc_size(shape[-1::-1], wg))
            shape = tuple(i // 2 for i in shape)


    def keypoints(self, image):
        """
        Calculates the keypoints of the image
        @param image: ndimage of 2D (or 3D if RGB)
        """
        self.reset_timer()
        with self._sem:
            total_size = 0
            keypoints = []
        #    descriptors = []
            assert image.shape[:2] == self.shape
            assert image.dtype == self.dtype
            t0 = time.time()

            if self.dtype == numpy.float32:
                if type(image) == pyopencl.array.Array:
                    evt = pyopencl.enqueue_copy(self.queue, self.buffers[0].data, image.data)
                else:
                    evt = pyopencl.enqueue_copy(self.queue, self.buffers[0].data, image)
                if self.profile:self.events.append(("copy H->D", evt))
            elif (len(image.shape) == 3) and (self.dtype == numpy.uint8) and (self.RGB):
                if type(image) == pyopencl.array.Array:
                    evt = pyopencl.enqueue_copy(self.queue, self.buffers["raw"].data, image.data)
                else:
                    evt = pyopencl.enqueue_copy(self.queue, self.buffers["raw"].data, image)
                if self.profile:self.events.append(("copy H->D", evt))
#                print self.procsize[0], self.wgsize[0]
                evt = self.programs["preprocess"].rgb_to_float(self.queue, self.procsize[0], self.wgsize[0],
                                                             self.buffers["raw"].data, self.buffers[0].data, *self.scales[0])
                if self.profile:self.events.append(("RGB -> float", evt))

            elif self.dtype in self.converter:
                program = self.programs["preprocess"].__getattr__(self.converter[self.dtype])
                evt = pyopencl.enqueue_copy(self.queue, self.buffers["raw"].data, image)
                if self.profile:self.events.append(("copy H->D", evt))
                evt = program(self.queue, self.procsize[0], self.wgsize[0],
                        self.buffers["raw"].data, self.buffers[0].data, *self.scales[0])
                if self.profile:self.events.append(("convert -> float", evt))
            else:
                raise RuntimeError("invalid input format error")

            '''k1 = self.programs["reductions"].max_min_global_stage1(self.queue, (self.red_size * self.red_size,), (self.red_size,),
                                                                   self.buffers[0].data,
                                                                   self.buffers["max_min"].data,
                                                                   numpy.uint32(self.shape[0] * self.shape[1]))
            k2 = self.programs["reductions"].max_min_global_stage2(self.queue, (self.red_size,), (self.red_size,),
                                                                   self.buffers["max_min"].data,
                                                                   self.buffers["max"].data,
                                                                   self.buffers["min"].data)
            if self.profile:
                self.events.append(("max_min_stage1", k1))
                self.events.append(("max_min_stage2", k2))'''
            '''evt = self.programs["preprocess"].normalizes(self.queue, self.procsize[0], self.wgsize[0],
                                                   self.buffers[0].data,
                                                   self.buffers["min"].data,
                                                   self.buffers["max"].data,
                                                   self.buffers["255"].data,
                                                   *self.scales[0])
            if self.profile:self.events.append(("normalize", evt))
'''

#            octSize = 1.0
#             self.debug.append(self.buffers[0].get())

            octave = 0
            if self._init_sigma > par.CurSigma:
                logger.debug("Bluring image to achieve std: %f", self._init_sigma)
                sigma = math.sqrt(self._init_sigma ** 2 - par.CurSigma ** 2)
                self._gaussian_convolution(self.buffers[0], self.buffers[0], sigma, 0)

            for octave in range(1):
                kp = self._one_octave(octave)


                if len(kp) > 0:
                    keypoints+=kp
                #    descriptors.append(descriptor)
                    for k in kp:
                        total_size += k.shape[0]
                logger.info("in octave %i found %i kp" % (octave, total_size))
            ########################################################################
            # Merge keypoints in central memory
            ########################################################################
            output = numpy.recarray(shape=(total_size,), dtype=self.dtype_kp)
            last = 0
            for ds in keypoints:
#                 print(ds)
                l = ds.shape[0]
                
                if l > 0:
                    output[last:last + l].x = ds[:, 0]
                    output[last:last + l].y = ds[:, 1]
                    output[last:last + l].scale = ds[:, 2]
                    output[last:last + l].angle = ds[:, 3]
                    last += l
            logger.info("Execution time: %.3fms" % (1000 * (time.time() - t0)))
    #        self.count_kp(output)
        return output
        
        

    def _gaussian_convolution(self, input_data, output_data, sigma, octave=0):
        """
        Calculate the gaussian convolution with precalculated kernels.

        @param input_data: pyopencl array with input
        @param output_data: pyopencl array with result
        @param sigma: width of the gaussian
        @param octave: related to the size on the input images

        * Uses a temporary buffer
        * Needs gaussian kernel to be available on device

        """
        temp_data = self.buffers["tmp"]
        gaussian = self.buffers["gaussian_%s" % sigma]
        k1 = self.programs["convolution"].horizontal_convolution(self.queue, self.procsize[octave], self.wgsize[octave],
                                input_data.data, temp_data.data, gaussian.data, numpy.int32(gaussian.size), *self.scales[octave])
        k2 = self.programs["convolution"].vertical_convolution(self.queue, self.procsize[octave], self.wgsize[octave],
                                temp_data.data, output_data.data, gaussian.data, numpy.int32(gaussian.size), *self.scales[octave])
#         self.debug.append(output_data.get())
        if self.profile:
            self.events += [("Blur sigma %s octave %s" % (sigma, octave), k1), ("Blur sigma %s octave %s" % (sigma, octave), k2)]

    def _one_octave(self, octave):
        """
        Does all scales within an octave

        @param octave: number of the octave
        """
        results=[]
        prevSigma = self._init_sigma
        logger.info("Calculating octave %i" % octave)
        wgsize = (128,)  # (max(self.wgsize[octave]),) #TODO: optimize
        kpsize32 = numpy.int32(self.kpsize)
        self._reset_keypoints()
        octsize = numpy.int32(2 ** octave)
        last_start = numpy.int32(0)     
        for scale in range(par.Scales + 2):
            if scale == 0 :
                absSigma = self._init_sigma
                sigma = self._init_sigma
            else :
                absSigma = self._init_sigma * self.sigmaRatio ** (1.0*scale/(par.Scales))
                sigma = prevSigma * math.sqrt(self.sigmaRatio ** (2.0/(par.Scales)) - 1.0)
            logger.info("Octave %i scale %s blur with sigma %s" % (octave, scale, sigma))
            print (absSigma,sigma)

            ########################################################################
            # Calculate gaussian blur and DoG
            ########################################################################

            self._gaussian_convolution(self.buffers[scale], self.buffers[scale + 1], sigma, octave)
            prevSigma = absSigma
            evt = self.programs["algebra"].combine(self.queue, self.procsize[octave], self.wgsize[octave],
                                             self.buffers[scale + 1].data, numpy.float32(-1.0),
                                             self.buffers[scale].data, numpy.float32(+1.0),
                                             self.buffers["DoGs"].data, numpy.int32(scale),
                                             *self.scales[octave])
            if self.profile:self.events.append(("DoG %s %s" % (octave, scale), evt))
        self.debug.append(self.buffers["DoGs"].get())
        for scale in range(1, par.Scales + 1):
#                print("Before local_maxmin, cnt is %s %s %s" % (self.buffers["cnt"].get()[0], self.procsize[octave], self.wgsize[octave]))
            evt = self.programs["image"].local_maxmin(self.queue, self.procsize[octave], self.wgsize[octave],
                                            self.buffers["DoGs"].data,      # __global float* DOGS,
                                            self.buffers["Kp_1"].data,      # __global keypoint* output,
                                            numpy.int32(par.BorderDist),    # int border_dist,
                                            octsize,                        # int octsize,
                                            self.buffers["cnt"].data,       # __global int* counter,
                                            kpsize32,                       # int nb_keypoints,
                                            numpy.int32(scale),             # int scale,
                                            *self.scales[octave])           # int width, int height)
            if self.profile:self.events.append(("local_maxmin %s %s" % (octave, scale), evt))
            
#                print("after local_max_min:")
#                print(self.buffers["Kp_1"].get()[:5])

#                self.debug_holes("After local_maxmin %s %s" % (octave, scale))

            procsize = calc_size((self.kpsize,), wgsize)
            
#           Refine keypoints
#             kp_counter = self.buffers["cnt"].get()[0]

            cp_evt = pyopencl.enqueue_copy(self.queue, self.cnt, self.buffers["cnt"].data)
            
#             kp_counter = self.cnt[0]
            # TODO: modify interp_keypoint so that it reads end_keypoint from GPU memory

#             evt = self.programs["image"].interp_keypoint(self.queue, procsize, wgsize,
#                                           self.buffers["DoGs"].data,        # __global float* DOGS,
#                                           self.buffers["Kp_1"].data,        # __global keypoint* keypoints,
#                                           last_start,                       # int start_keypoint,
#                                           self.cnt[0],                      # int end_keypoint,
#                                           numpy.float32(self._init_sigma),     # float InitSigma,
#                                           numpy.int32(par.Scales),          #int Scales
#                                           *self.scales[octave])             # int width, int height)
#             if self.profile:
#                 self.events += [("get cnt", cp_evt),
#                                 ("interp_keypoint %s %s" % (octave, scale), evt)
#                                 ]
#   
#                 self.debug_holes("After interp_keypoint %s %s" % (octave, scale))
            newcnt = self._compact()        
            print("octave %i scale %i kp %i"%(octave, scale, newcnt))
            if newcnt:
                result = numpy.empty((newcnt, 4), dtype=numpy.float32)
                evt = pyopencl.enqueue_copy(self.queue, result, self.buffers["Kp_1"].data)
                evt.wait()
#                 print (result) 
                results.append(result)
                if self.profile:
                    self.event.append(("copy D->H", evt))
                self._reset_keypoints()
 #           
        
        ########################################################################
        # Rescale all images to populate all octaves
        ########################################################################
#         if octave < self.octave_max - 1:
#             evt = self.programs["preprocess"].shrink(self.queue, self.procsize[octave + 1], self.wgsize[octave + 1],
#                                                     self.buffers[par.Scales].data,
#                                                     self.buffers[0].data,
#                                                     numpy.int32(2), numpy.int32(2),
#                                                     self.scales[octave][0], self.scales[octave][1],
#                                                     *self.scales[octave + 1])
#             if self.profile:
#                 self.events.append(("shrink %s->%s" % (self.scales[octave], self.scales[octave + 1]), evt))

        return results 

    def _compact(self, start=numpy.int32(0)):
        """
        Compact the vector of keypoints starting from start

        @param start: start compacting at this adress. Before just copy
        @type start: numpy.int32
        """
        wgsize = self.max_workgroup_size,  # (max(self.wgsize[0]),) #TODO: optimize
        kpsize32 = numpy.int32(self.kpsize)
#        kp_counter = self.buffers["cnt"].get()[0]
        cp0_evt = pyopencl.enqueue_copy(self.queue, self.cnt, self.buffers["cnt"].data)
        kp_counter = self.cnt[0]
        procsize = calc_size((self.kpsize,), wgsize)

        if kp_counter > 0.9 * self.kpsize:
            logger.warning("Keypoint counter overflow risk: counted %s / %s" % (kp_counter, self.kpsize))
        logger.info("Compact %s -> %s / %s" % (start, kp_counter, self.kpsize))
#        self.buffers["cnt"].set(numpy.array([start], dtype=numpy.int32))
        self.cnt[0] = start
        cp1_evt = pyopencl.enqueue_copy(self.queue, self.buffers["cnt"].data, self.cnt)
        evt = self.programs["algebra"].compact(self.queue, procsize, wgsize,
                        self.buffers["Kp_1"].data,  # __global keypoint* keypoints,
                        self.buffers["Kp_2"].data,  # __global keypoint* output,
                        self.buffers["cnt"].data,  # __global int* counter,
                        start,  # int start,
                        kp_counter)  # int nbkeypoints
#        newcnt = self.buffers["cnt"].get()[0]
        cp2_evt = pyopencl.enqueue_copy(self.queue, self.cnt, self.buffers["cnt"].data)
        # print("After compaction, %i (-%i)" % (newcnt, kp_counter - newcnt))
        # swap keypoints:
        self.buffers["Kp_1"], self.buffers["Kp_2"] = self.buffers["Kp_2"], self.buffers["Kp_1"]
        # memset buffer Kp_2
#        self.buffers["Kp_2"].fill(-1, self.queue)
        mem_evt = self.programs["memset"].memset_float(self.queue, calc_size((4 * self.kpsize,), wgsize), wgsize, self.buffers["Kp_2"].data, numpy.float32(-1), numpy.int32(4 * self.kpsize))
        if self.profile:
            self.events += [("copy cnt D->H", cp0_evt),
                            ("copy cnt H->D", cp1_evt),
                            ("compact", evt),
                            ("copy cnt D->H", cp2_evt),
                            ("memset 2", mem_evt)
                            ]
        return self.cnt[0]


    def _reset_keypoints(self):
        """
        Todo: implement directly in OpenCL instead of relying on pyOpenCL
        """
        wg_size = self.kernels["memset"],
        evt1 = self.programs["memset"].memset_float(self.queue, calc_size((4 * self.kpsize,), wg_size), wg_size, self.buffers["Kp_1"].data, numpy.float32(-1), numpy.int32(4 * self.kpsize))
#        evt2 = self.programs["memset"].memset_float(self.queue, calc_size((4 * self.kpsize,), wg_size), wg_size, self.buffers["Kp_2"].data, numpy.float32(-1), numpy.int32(4 * self.kpsize))
        evt3 = self.programs["memset"].memset_int(self.queue, (1,), (1,), self.buffers["cnt"].data, numpy.int32(0), numpy.int32(1))
        if self.profile:
            self.events += [("memset 1", evt1), ("memset cnt", evt3)]
#        self.buffers["Kp_1"].fill(-1, self.queue)
#        self.buffers["Kp_2"].fill(-1, self.queue)
#        self.buffers["cnt"].fill(0, self.queue)

    def count_kp(self, output):
        """
        Print the number of keypoint per octave
        """
        kpt = 0
        for octave, data in enumerate(output):
            if output.shape[0] > 0:
                ksum = (data[:, 1] != -1.0).sum()
                kpt += ksum
                print("octave %i kp count %i/%i size %s ratio:%s" % (octave, ksum, self.kpsize, self.scales[octave], 1000.0 * ksum / self.scales[octave][1] / self.scales[octave][0]))
        print("Found total %i guess %s pixels per keypoint" % (kpt, self.shape[0] * self.shape[1] / kpt))

    def debug_holes(self, label=""):
        print("%s %s" % (label, numpy.where(self.buffers["Kp_1"].get()[:, 1] == -1)[0]))

    def log_profile(self):
        """
        If we are in debugging mode, prints out all timing for every single OpenCL call
        """
        t = 0.0
        #orient = 0.0
        #descr = 0.0
        if self.profile:
            for e in self.events:
                if "__len__" in dir(e) and len(e) >= 2:
                    et = 1e-6 * (e[1].profile.end - e[1].profile.start)
                    print("%50s:\t%.3fms" % (e[0], et))
                    t += et
         #           if "orient" in e[0]:
          #              orient += et
           #         if "descriptors" in e[0]:
            #            descr += et

        print("_"*80)
        print("%50s:\t%.3fms" % ("Total execution time", t))
        #print("%50s:\t%.3fms" % ("Total Orientation assignment", orient))
        #print("%50s:\t%.3fms" % ("Total Descriptors", descr))

    def reset_timer(self):
        """
        Resets the profiling timers
        """
        with self._sem:
            self.events = []

if __name__ == "__main__":
    # Prepare debugging
    import scipy.misc
    lena = scipy.misc.lena()
    s = SiftPlan(template=lena)
    s.keypoints(lena)






