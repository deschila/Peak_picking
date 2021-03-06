#!/usr/bin/python
# -*- coding: utf8 -*
"""
Small demonstration for sift using pyOpenCL
"""
from __future__ import division

__authors__ = ["Jérôme Kieffer"]
__contact__ = "jerome.kieffer@esrf.eu"
__copyright__ = "European Synchrotron Radiation Facility, Grenoble, France"
__date__ = "2013-07-15"
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

import sys, os, pyopencl, time
from math import sin, cos
import utilstest
logger = utilstest.getLogger(__file__)
import peak_picking
import numpy
import scipy.misc
import pylab
try:
    import feature
except:
    logger.error("Feature is not available to compare results with C++ implementation")
    feature = None
    res = numpy.empty(0)

def cmp_kp(a, b):
    if a.scale < b.scale:
        return True
    elif a.scale > b.scale:
        return False
    else:
        if a.angle > b.angle:
            return True
        else:
            return False


class DemoSift(object):
    def __init__(self, filename=None, devicetype=None, device=None, context=None, profile=False):
        if filename and os.path.exists(filename):
            self.filename = filename
        else:
            self.filename = "LaB6_0003.mar3450"
#             self.filename = utilstest.UtilsTest.getimage("wikipedia/commons/9/94/Esrf_grenoble.jpg")

        try:
            self.image_rgb = scipy.misc.imread(self.filename)
        except:
            import fabio
            self.image_rgb = fabio.open(self.filename).data
        if self.image_rgb.ndim != 2:
            self.image_bw = 0.299 * self.image_rgb[:, :, 0] + 0.587 * self.image_rgb[:, :, 1] + 0.114 * self.image_rgb[:, :, 2]
        else: self.image_bw = self.image_rgb
        if feature:
            self._sift_cpp = feature.SiftAlignment()
        self._sift_ocl = peak_picking.SiftPlan(template=self.image_rgb, device=device, devicetype=devicetype, context=context, profile=profile)
        print self._sift_ocl
        print peak_picking
        self.ctx = self._sift_ocl.ctx
        self.kp_cpp = numpy.empty(0)
        self.kp_ocl = numpy.empty(0)
        self.fig = pylab.figure()
        self.sp1 = self.fig.add_subplot(1, 1, 1)
        self.im1 = self.sp1.imshow(numpy.log1p(self.image_rgb))
        self.sp1.set_title("OpenCL: %s keypoint" % self.kp_ocl.size)
        #self.sp2 = self.fig.add_subplot(1, 2, 2)
        #self.im2 = self.sp2.imshow(self.image_bw, cmap="gray")
        #self.sp2.set_title("C++: %s keypoint" % self.kp_cpp.size)
        self.fig.show()
        self.timing_cpp = None
        self.timing_ocl = None

    def sift_cpp(self):
        print(os.linesep + "Running SIFT using C++ code")
        t0 = time.time()
        self.kp_cpp = self._sift_cpp.sift(self.image_bw)
        t1 = time.time()
        self.timing_cpp = t1 - t0
        if "size" not in dir(self.kp_cpp):
            return #we are using an old kind of Sift-C++
        self.sp2.set_title("C++: %s keypoint" % self.kp_cpp.size)
        self.fig.canvas.draw()
#        self.kp_cpp.sort(order=["scale", "angle", "x", "y"])

    def sift_ocl(self):
        print(os.linesep + "Running SIFT using OpenCL code")
        t0 = time.time()
        self.kp_ocl = self._sift_ocl.keypoints(self.image_rgb)
        t1 = time.time()
        self.timing_ocl = t1 - t0
        self.sp1.set_title("OpenCL: %s keypoint" % self.kp_ocl.size)
        self.fig.canvas.draw()
#        self.kp_ocl.sort(order=["scale", "angle", "x", "y"])

    def timings(self):
        if self.kp_ocl.size > 0 and self.kp_cpp > 0:
            print("Computing time using C++: %.3fms\t using OpenCL: %.3fms:\t Speed up: %.3f" % (1e3 * self.timing_cpp, 1e3 * self.timing_ocl, self.timing_cpp / self.timing_ocl))

    def show(self, max_kp=None):
#         for x,y in zip(self.kp_ocl.x[:max_kp],self.kp_ocl.y[:max_kp]):
#             print x,y
        self.sp1.plot(self.kp_ocl.x[:max_kp], self.kp_ocl.y[:max_kp], "or")
#        if max_kp == None:
#            max_kp = sys.maxint
#        if self.kp_cpp.size > max_kp:
#            print("keeping only the %i largest keypoints for display" % max_kp)

#        todo = min(self.kp_ocl.size, max_kp)
#        for i in range(self.kp_ocl.size - todo, self.kp_ocl.size):
#            x = self.kp_ocl[i].x
#            y = self.kp_ocl[i].y
#            scale = self.kp_ocl[i].scale
#            angle = self.kp_ocl[i].angle
#            x0 = x + scale * cos(angle)
#            y0 = y + scale * sin(angle)
#            self.sp1.annotate("", xy=(x, y), xytext=(x0, y0), color="blue",
#                             arrowprops=dict(facecolor='blue', edgecolor='blue', width=1),)
        self.fig.canvas.draw()

    def match(self):
        if self.kp_ocl.size > 0 and self.kp_cpp > 0:
            t0 = time.time()
            match = feature.sift_match(self.kp_ocl, self.kp_cpp)
            t1 = time.time()
            print("Number of Keypoints with OpenCL: %i, With C++: %i, Matching keypoints: %i, Processing time: %.3fs" % (self.kp_ocl.size, self.kp_cpp.size, match.shape[0], (t1 - t0)))
            self.fig.canvas.draw()
        else:
            print("No keypoints, cannot match")


if __name__ == "__main__":
#    from optparse import OptionParser
#    parser = OptionParser(version="1.0", description="Demonstration of sift using OpenCL version C++ implementation",
#                          usage="usage: %prog [options] imagefiles*")
#
#    parser.add_option("-d", "--device", dest="device",
#                  help="device on which to run: coma separated like --device=0,1",
#                  default=None)
#    parser.add_option("-t", "--type", dest="type",
#                       help="device type  on which to run like CPU (default) or GPU",
#                       default="CPU")
#    parser.add_option("-p", "--profile", dest="profile",
#                       help="Print profiling information of OpenExecution",
#                       default=False, action="store_true")
#    parser.add_option("-f", "--force", dest="force",
#                       help="rebuild the package",
#                       default=False, action="store_true")
#    parser.add_option("-r", "--remove", dest="remove",
#                       help="remove build and rebuild the package",
#                       default=False, action="store_true")
#    (options, args) = parser.parse_args()
    options = utilstest.options
    args = utilstest.args
    if args:
        for i in args:
            if os.path.exists(i):
                print("Processing file %s" % i)
                d = DemoSift(i, context=utilstest.ctx, profile=options.info)
                d.sift_ocl()
                if feature:
                    d.sift_cpp()
                d.timings()
                d.show(100)
                d.match()
                if options.info:
                    d._sift_ocl.log_profile()
                raw_input()
    else:
        print("Processing file demo image")
        d = DemoSift(devicetype="CPU",profile=options.info,)#context=utilstest.ctx, )
        d.sift_ocl()
#        if feature:
#            d.sift_cpp()
#        d.timings()
        d.show()
#        d.match()
        if options.info:
            d._sift_ocl.log_profile()
        raw_input()

