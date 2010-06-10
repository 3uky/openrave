#!/usr/bin/env python
# Copyright (C) 2009-2010 Rosen Diankov (rosen.diankov@gmail.com)
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import with_statement # for python 2.5
__author__ = 'Rosen Diankov'
__copyright__ = 'Copyright (C) 2009-2010 Rosen Diankov (rosen.diankov@gmail.com)'
__license__ = 'Apache License, Version 2.0'

import openravepy
from openravepy import *
from openravepy import pyANN
from openravepy.databases import convexdecomposition,inversekinematics
from numpy import *
import time
import heapq # for nth smallest element
from optparse import OptionParser

class ReachabilityModel(OpenRAVEModel):
    """Computes the robot manipulator's reachability space (stores it in 6D) and
    offers several functions to use it effectively in planning."""

    class QuaternionKDTree(metaclass.AutoReloader):
        """Artificially add more weight to the X,Y,Z translation dimensions"""
        def __init__(self, poses,transmult):
            self.numposes = len(poses)
            self.transmult = transmult
            self.itransmult = 1/transmult
            searchposes = array(poses)
            searchposes[:,4:] *= self.transmult # take translation errors more seriously
            allposes = r_[searchposes,searchposes]
            allposes[self.numposes:,0:4] *= -1
            self.nnposes = pyANN.KDTree(allposes)
        def kSearch(self,poses,k,eps):
            """returns distance squared"""
            poses[:,4:] *= self.transmult
            neighs,dists = self.nnposes.kSearch(poses,k,eps)
            neighs[neighs>=self.numposes] -= self.numposes
            poses[:,4:] *= self.itransmult
            return neighs,dists
        def kFRSearch(self,pose,radiussq,k,eps):
            """returns distance squared"""
            pose[4:] *= self.transmult
            neighs,dists,kball = self.nnposes.kFRSearch(pose,radiussq,k,eps)
            neighs[neighs>=self.numposes] -= self.numposes
            pose[4:] *= self.itransmult
            return neighs,dists,kball
        def kFRSearchArray(self,poses,radiussq,k,eps):
            """returns distance squared"""
            poses[:,4:] *= self.transmult
            neighs,dists,kball = self.nnposes.kFRSearchArray(poses,radiussq,k,eps)
            neighs[neighs>=self.numposes] -= self.numposes
            poses[:,4:] *= self.itransmult
            return neighs,dists,kball

    def __init__(self,robot):
        OpenRAVEModel.__init__(self,robot=robot)
        self.ikmodel = inversekinematics.InverseKinematicsModel(robot=robot,iktype=IkParameterization.Type.Transform6D)
        if not self.ikmodel.load():
            self.ikmodel.autogenerate()
        self.reachabilitystats = None
        self.reachability3d = None
        self.reachabilitydensity3d = None
        self.pointscale = None
        self.xyzdelta = None
        self.quatdelta = None
        self.kdtree6d = None
        self.kdtree3d = None
    def clone(self,envother):
        clone = OpenRAVEModel.clone(self,envother)
        return clone
    def has(self):
        return len(self.reachabilitydensity3d) > 0 and len(self.reachability3d) > 0
    def getversion(self):
        return 4
    def load(self):
        try:
            params = OpenRAVEModel.load(self)
            if params is None:
                return False
            self.reachabilitystats,self.reachabilitydensity3d,self.reachability3d,self.pointscale,self.xyzdelta,self.quatdelta = params
            return self.has()
        except e:
            return False
    def save(self):
        OpenRAVEModel.save(self,(self.reachabilitystats,self.reachabilitydensity3d,self.reachability3d, self.pointscale,self.xyzdelta,self.quatdelta))

    def getfilename(self):
        return os.path.join(OpenRAVEModel.getfilename(self),'reachability.' + self.manip.GetStructureHash() + '.pp')

    def autogenerate(self,options=None):
        maxradius=None
        translationonly=False
        xyzdelta=None
        quatdelta=None
        usefreespace=True
        useconvex=False
        if options is not None:
            if options.maxradius is not None:
                maxradius = options.maxradius
            if options.xyzdelta is not None:
                xyzdelta=options.xyzdelta
            if options.quatdelta is not None:
                quatdelta=options.quatdelta
            usefreespace=options.usefreespace
            useconvex=options.useconvex
        if self.robot.GetKinematicsGeometryHash() == '0d258d45aacb7ea4f6f88c4602d4b077' or self.robot.GetKinematicsGeometryHash() == '2c7f45a52ae3cbd4c0663d8abbd5f020': # wam
            if maxradius is None:
                maxradius = 1.1
        elif self.robot.GetKinematicsGeometryHash() == 'bec5e13f7bc7f7fcc3e07e8a82522bee': # pr2
            if xyzdelta is None:
                xyzdelta = 0.03
            if quatdelta is None:
                quatdelta = 0.2
        self.generate(maxradius=maxradius,translationonly=translationonly,xyzdelta=xyzdelta,quatdelta=quatdelta,usefreespace=usefreespace,useconvex=useconvex)
        self.save()

    def getOrderedArmJoints(self):
        return [j for j in self.robot.GetDependencyOrderedJoints() if j.GetJointIndex() in self.manip.GetArmJoints()]
    @staticmethod
    def getManipulatorLinks(manip):
        links = manip.GetChildLinks()
        # add the links connecting to the base link.... although this reduces the freespace of the arm, it is better to have than not (ie waist on humanoid)
        tobasejoints = manip.GetRobot().GetChain(0,manip.GetBase().GetIndex())
        if len(tobasejoints) > 0:
            tobasedofs = hstack([arange(joint.GetDOFIndex(),joint.GetDOFIndex()+joint.GetDOF()) for joint in tobasejoints])
        else:
            tobasedofs = []
        robot = manip.GetRobot()
        joints = robot.GetJoints()
        for jindex in r_[manip.GetArmJoints(),tobasedofs]:
            joint = joints[jindex]
            if joint.GetFirstAttached() and not joint.GetFirstAttached() in links:
                links.append(joint.GetFirstAttached())
            if joint.GetSecondAttached() and not joint.GetSecondAttached() in links:
                links.append(joint.GetSecondAttached())
        # don't forget the rigidly attached links
        for link in links[:]:
            for newlink in robot.GetRigidlyAttachedLinks(link.GetIndex()):
                if not newlink in links:
                    links.append(newlink)
        return links
    def generate(self,maxradius=None,translationonly=False,xyzdelta=None,quatdelta=None,usefreespace=True,useconvex=False):
        # disable every body but the target and robot
        self.kdtree3d = self.kdtree6d = None
        bodies = [(b,b.IsEnabled()) for b in self.env.GetBodies() if b != self.robot]
        for b in bodies:
            b[0].Enable(False)
        try:
            if xyzdelta is None:
                xyzdelta=0.04
            if quatdelta is None:
                quatdelta=0.5
            starttime = time.time()
            with self.robot:
                self.robot.SetTransform(eye(4))
                if useconvex:
                    self.cdmodel = convexdecomposition.ConvexDecompositionModel(self.robot)
                    if not self.cdmodel.load():
                        self.cdmodel.autogenerate()
                Tbase = self.manip.GetBase().GetTransform()
                Tbaseinv = linalg.inv(Tbase)
                maniplinks = self.getManipulatorLinks(self.manip)
                for link in self.robot.GetLinks():
                    link.Enable(link in maniplinks)
                # the axes' anchors are the best way to find the max radius
                # the best estimate of arm length is to sum up the distances of the anchors of all the points in between the chain
                armjoints = self.getOrderedArmJoints()
                baseanchor = transformPoints(Tbaseinv,[armjoints[0].GetAnchor()])
                eetrans = self.manip.GetEndEffectorTransform()[0:3,3]
                armlength = 0
                for j in armjoints[::-1]:
                    armlength += sqrt(sum((eetrans-j.GetAnchor())**2))
                    eetrans = j.GetAnchor()    
                if maxradius is None:
                    maxradius = armlength+xyzdelta

                allpoints,insideinds,shape,self.pointscale = self.UniformlySampleSpace(maxradius,delta=xyzdelta)
                qarray = SpaceSampler().sampleSO3(quatdelta=quatdelta)
                rotations = [eye(3)] if translationonly else rotationMatrixFromQArray(qarray)
                self.xyzdelta = xyzdelta
                self.quatdelta = 0
                if not translationonly:
                    # for rotations, get the average distance to the nearest rotation
                    neighdists = []
                    for q in qarray:
                        neighdists.append(heapq.nsmallest(2,quatArrayTDist(q,qarray))[1])
                    self.quatdelta = mean(neighdists)
                print 'radius: %f, xyzsamples: %d, quatdelta: %f, rot samples: %d, freespace: %d'%(maxradius,len(insideinds),self.quatdelta,len(rotations),usefreespace)

                T = eye(4)
                reachabilitydensity3d = zeros(prod(shape))
                reachability3d = zeros(prod(shape))
                self.reachabilitystats = []
                with self.env:
                    for i,ind in enumerate(insideinds):
                        numvalid = 0
                        numrotvalid = 0
                        T[0:3,3] = allpoints[ind]+baseanchor
                        for rotation in rotations:
                            T[0:3,0:3] = rotation
                            if usefreespace:
                                solutions = self.manip.FindIKSolutions(dot(Tbase,T),False) # do not want to include the environment
                                if solutions is not None:
                                    self.reachabilitystats.append(r_[poseFromMatrix(T),len(solutions)])
                                    numvalid += len(solutions)
                                    numrotvalid += 1
                            else:
                                solution = self.manip.FindIKSolution(dot(Tbase,T),False)
                                if solution is not None:
                                    self.reachabilitystats.append(r_[poseFromMatrix(T),1])
                                    numvalid += 1
                                    numrotvalid += 1
                        if mod(i,1000)==0:
                            print '%d/%d'%(i,len(insideinds))
                        reachabilitydensity3d[ind] = numvalid/float(len(rotations))
                        reachability3d[ind] = numrotvalid/float(len(rotations))
                self.reachability3d = reshape(reachability3d,shape)
                self.reachabilitydensity3d = reshape(reachabilitydensity3d,shape)
                self.reachabilitystats = array(self.reachabilitystats)
                print 'reachability finished in %fs'%(time.time()-starttime)
        finally:
            for b,enable in bodies:
                b.Enable(enable)

    def show(self,showrobot=True,contours=[0.01,0.1,0.2,0.5,0.8,0.9,0.99],opacity=None,figureid=1, xrange=None,options=None):
        mlab.figure(figureid,fgcolor=(0,0,0), bgcolor=(1,1,1),size=(1024,768))
        mlab.clf()
        print 'max reachability: ',numpy.max(self.reachability3d)
        if options is not None:
            reachability3d = minimum(self.reachability3d*options.showscale,1.0)
        else:
            reachability3d = minimum(self.reachability3d,1.0)
        reachability3d[0,0,0] = 1 # have at least one point be at the maximum
        if xrange is None:
            offset = array((0,0,0))
            src = mlab.pipeline.scalar_field(reachability3d)
        else:
            offset = array((xrange[0]-1,0,0))
            src = mlab.pipeline.scalar_field(r_[zeros((1,)+reachability3d.shape[1:]),reachability3d[xrange,:,:],zeros((1,)+reachability3d.shape[1:])])
            
        for i,c in enumerate(contours):
            mlab.pipeline.iso_surface(src,contours=[c],opacity=min(1,0.7*c if opacity is None else opacity[i]))
        #mlab.pipeline.volume(mlab.pipeline.scalar_field(reachability3d*100))
        if showrobot:
            with self.robot:
                Tbase = self.manip.GetBase().GetTransform()
                Tbaseinv = linalg.inv(Tbase)
                self.robot.SetTransform(dot(Tbaseinv,self.robot.GetTransform()))
                baseanchor = self.getOrderedArmJoints()[0].GetAnchor()
                trimesh = self.env.Triangulate(self.robot)
            v = self.pointscale[0]*(trimesh.vertices-tile(baseanchor,(len(trimesh.vertices),1)))+self.pointscale[1]
            mlab.triangular_mesh(v[:,0]-offset[0],v[:,1]-offset[1],v[:,2]-offset[2],trimesh.indices,color=(0.5,0.5,0.5))
        mlab.show()

    def UniformlySampleSpace(self,maxradius,delta):
        nsteps = floor(maxradius/delta)
        X,Y,Z = mgrid[-nsteps:nsteps,-nsteps:nsteps,-nsteps:nsteps]
        allpoints = c_[X.flat,Y.flat,Z.flat]*delta
        insideinds = flatnonzero(sum(allpoints**2,1)<maxradius**2)
        return allpoints,insideinds,X.shape,array((1.0/delta,nsteps))

    def ComputeNN(self,translationonly=False):
        if translationonly:
            if self.kdtree3d is None:
                self.kdtree3d = pyANN.KDTree(self.reachabilitystats[:,4:7])
            return self.kdtree3d
        else:
            if self.kdtree6d is None:
                self.kdtree6d = self.QuaternionKDTree(self.reachabilitystats,5.0)
            return self.kdtree6d
    @staticmethod
    def CreateOptionParser():
        parser = OpenRAVEModel.CreateOptionParser()
        parser.description='Computes the reachability region of a robot manipulator and python pickles it into a file.'
        parser.add_option('--maxradius',action='store',type='float',dest='maxradius',default=None,
                          help='The max radius of the arm to perform the computation')
        parser.add_option('--xyzdelta',action='store',type='float',dest='xyzdelta',default=None,
                          help='The max radius of the arm to perform the computation (default=0.04)')
        parser.add_option('--quatdelta',action='store',type='float',dest='quatdelta',default=None,
                          help='The max radius of the arm to perform the computation (default=0.5)')
        parser.add_option('--ignorefreespace',action='store_false',dest='usefreespace',default=True,
                          help='If set, will only check if at least one IK solutions exists for every transform rather that computing a density')
        parser.add_option('--useconvex',action='store_true',dest='useconvex',default=False,
                          help='If set, will use the convex decomposition of the robot for kinematic reachability (this might cause self-collisions undesired places)')
        parser.add_option('--showscale',action='store',type='float',dest='showscale',default=1.0,
                          help='Scales the reachability by this much in order to show colors better (default=%default)')
        return parser
    @staticmethod
    def RunFromParser(Model=None,parser=None):
        if parser is None:
            parser = ReachabilityModel.CreateOptionParser()
        (options, args) = parser.parse_args()
        env = Environment()
        try:
            if Model is None:
                Model = lambda robot: ReachabilityModel(robot=robot)
            OpenRAVEModel.RunFromParser(env=env,Model=Model,parser=parser)
        finally:
            env.Destroy()

if __name__=='__main__':
    parser = ReachabilityModel.CreateOptionParser()
    (options, args) = parser.parse_args()
    if options.show: # only load mayavi if showing
        try:
            from enthought.mayavi import mlab
        except ImportError:
            pass
    ReachabilityModel.RunFromParser()