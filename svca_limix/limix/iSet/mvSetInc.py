import sys
import limix
from limix.core.covar import LowRankCov
from limix.core.covar import FixedCov
from limix.core.covar import FreeFormCov
from limix.core.covar import CategoricalLR
from limix.core.mean import MeanBase
from limix.core.gp import GP 
import scipy as sp
import scipy.stats as st
from limix.mtSet.core.iset_utils import * 
import numpy as np
import numpy.linalg as nla
import scipy.linalg as la
import copy
import pdb
from limix.utils.preprocess import gaussianize
from scipy.optimize import fmin
import time
import pandas as pd
from .linalg_utils import msqrt
from .linalg_utils import lowrank_approx

ntype_dict = {'assoc':'null', 'gxe':'block', 'gxehet':'rank1'}

def define_gp(Y, Xr, mean, Ie, type):
    P = 2
    if type=='null':
        _Cr = FixedCov(sp.ones([2, 2]))
        _Cr.scale = 1e-9
        _Cr.act_scale = False
        covar = CategoricalLR(_Cr, sp.ones((Xr.shape[0], 1)), Ie)
    else:
        if type=='block':         _Cr = FixedCov(sp.ones((P,P)))
        elif type=='rank1':         _Cr = LowRankCov(P,1)
        elif type=='full':          _Cr = FreeFormCov(P)
        else:                       print('poppo')
        covar = CategoricalLR(_Cr, Xr, Ie)
    _gp = GP(covar=covar, mean=mean)
    return _gp

class MvSetTestInc():

    def __init__(self, Y=None, Xr=None, F=None, factr=1e7, Ie=None, debug=False):
        """
        Args:
            Y:          [N, 1] phenotype matrix
            Xr:         [N, S] genotype data of the set component
            R:          [N, S] genotype data of the set component
            factr:      paramenter that determines the accuracy of the solution
                        (see scipy.optimize.fmin_l_bfgs_b for more details)
        """
        if F is None:
            F = sp.ones((y.shape[0], 1))
        # kroneckerize F
        W = sp.zeros((Y.shape[0], 2*F.shape[1]))
        W[:, :F.shape[1]] = Ie[:, sp.newaxis] * F 
        W[:, F.shape[1]:] = (~Ie[:, sp.newaxis]) * F 
        self.mean = MeanBase(Y, W)
        # avoid SVD failus by adding some jitter
        Xr+= 2e-6*(sp.rand(*Xr.shape)-0.5)
        # store stuff 
        Xr-= Xr.mean(0)
        Xr/= Xr.std(0)
        Xr/= sp.sqrt(Xr.shape[1])
        self.Y = Y
        self.F = F
        self.Xr = Xr
        self.Ie  = Ie
        self.covY = sp.cov(Y.T)
        self.factr = factr 
        self.debug = debug
        self.gp = {}
        self.info = {}

    def assoc(self):
        # fit model 
        for key in ['null', 'full']:
            if key not in list(self.gp.keys()):
                if self.debug:      print('.. dening %s' % key)
                self.gp[key] = define_gp(self.Y, self.Xr, self.mean, self.Ie, key)
                if self.debug:      print('.. fitting %s' % key)
                self.info[key] = self._fit(key, vc=True)
        return self.info['null']['LML']-self.info['full']['LML']

    def gxe(self):
        # fit model 
        for key in ['null', 'full', 'block']:
            if key not in list(self.gp.keys()):
                if self.debug:      print('.. defining %s' % key)
                self.gp[key] = define_gp(self.Y, self.Xr, self.mean, self.Ie, key)
                if self.debug:      print('.. fitting %s' % key)
                self.info[key] = self._fit(key, vc=True)
        return self.info['block']['LML']-self.info['full']['LML']

    def gxehet(self):
        # fit model
        for key in ['null', 'full', 'rank1']:
            if key not in list(self.gp.keys()):
                if self.debug:      print('.. defining %s' % key)
                self.gp[key] = define_gp(self.Y, self.Xr, self.mean, self.Ie, key)
                if self.debug:      print('.. fitting %s' % key)
                self.info[key] = self._fit(key, vc=True)
        return self.info['rank1']['LML']-self.info['full']['LML']

    def assoc_null(self, n_nulls=30):
        LLR0 = sp.zeros(n_nulls)
        for ni in range(n_nulls):
            idx_perms = sp.random.permutation(self.Y.shape[0])
            _Xr = self.Xr[idx_perms]
            mvset0 = MvSetTestInc(Y=self.Y, F=self.F, Xr=_Xr, Ie=self.Ie)
            LLR0[ni] = mvset0.assoc()
        return LLR0

    def gxe_null(self, n_nulls=30):
        LLR0 = sp.zeros(n_nulls)
        for ni in range(n_nulls):
            Xb = sp.dot(self.mean.W, self.mean.b)
            _Y = Xb+self.gp['block'].covar.Kh_dot(sp.randn(self.Y.shape[0],1))
            mvset0 = MvSetTestInc(Y=_Y, F=self.F, Xr=self.Xr, Ie=self.Ie)
            LLR0[ni] = mvset0.gxe()
        return LLR0

    def gxehet_null(self, n_nulls=30):
        LLR0 = sp.zeros(n_nulls)
        for ni in range(n_nulls):
            Xb = sp.dot(self.mean.W, self.mean.b)
            _Y = Xb+self.gp['rank1'].covar.Kh_dot(sp.randn(self.Y.shape[0],1))
            mvset0 = MvSetTestInc(Y=_Y, F=self.F, Xr=self.Xr, Ie=self.Ie)
            LLR0[ni] = mvset0.gxehet()
        return LLR0

    def _fit(self, type, vc=False):
        #2. init
        if type=='null':
            self.gp[type].covar.Cn.setCovariance(sp.eye(2))
        elif type=='full':
            Cr0_K = 1e-4*sp.ones((2,2))+1e-4*sp.eye(2)
            Cn0_K = 0.99*self.gp['null'].covar.Cn.K()
            self.gp[type].covar.Cr.setCovariance(Cr0_K)
            self.gp[type].covar.Cn.setCovariance(Cn0_K)
        elif type=='block':
            Crf_K = self.gp['full'].covar.Cr.K()
            Cnf_K = self.gp['full'].covar.Cn.K()
            self.gp[type].covar.Cr.scale = sp.mean(Crf_K)
            self.gp[type].covar.Cn.setCovariance(Cnf_K)
        elif type=='rank1':
            Crf_K = self.gp['full'].covar.Cr.K()
            Cnf_K = self.gp['full'].covar.Cn.K()
            self.gp[type].covar.Cr.setCovariance(Crf_K)
            self.gp[type].covar.Cn.setCovariance(Cnf_K)
        else:
            print('poppo')
        conv = self.gp[type].optimize(factr=self.factr, verbose=False)[0]
        B = self.gp[type].mean.b.reshape((self.mean.W.shape[1]/2,2), order='F')
        RV = {'Cr': self.gp[type].covar.Cr.K(),      
                'Cn': self.gp[type].covar.Cn.K(),
                'B': B,
                'conv': sp.array([conv]),
                'LML': sp.array([self.gp[type].LML()]),
                'LMLgrad': sp.array([sp.mean((self.gp[type].LML_grad()['covar'])**2)])}
        if vc:
            # tr(P WW) = tr(PWWP) = ((PW)**2).sum()
            # tr(P D) = (PD).sum() = D.sum() - 1/n * (Ones*D).sum()
            #                      = D.sum() - D.sum() 
            PW = self.gp[type].covar.W()
            PW-= PW.mean(0) 
            var_r = (PW**2).sum()/ float(self.Y.size-1)
            var_c = sp.var(sp.dot(self.mean.W, self.gp[type].mean.b))
            D = self.gp[type].covar.d_inv()**(-1)
            var_n = (1-1/float(D.shape[0]))*D.sum()/float(self.Y.size-1) 
            #var_n = sp.diagonal(sp.diag(D)-sp.diag(D).mean(0)).sum()/float(self.Y.size-1)
            RV['var'] = sp.array([var_r, var_c, var_n])
            if 0 and self.Y.size<5000:
                pdb.set_trace()
                Kr = sp.kron(RV['Cr'], sp.dot(self.Xr, self.Xr.T))
                Kn = sp.kron(RV['Cn'], sp.eye(self.Y.shape[0]))
                _var_r = sp.trace(Kr-Kr.mean(0)) / float(self.Y.size-1)
                _var_n = sp.trace(Kn-Kn.mean(0)) / float(self.Y.size-1)
                _var = sp.array([_var_r, var_c, _var_n])
                print(((_var-RV['var'])**2).mean())
            if type=='full':
                trRr = (self.Xr**2).sum()
                # calculate within region vcs 
                Cr_block = sp.mean(RV['Cr']) * sp.ones(RV['Cr'].shape)
                Cr_rank1 = lowrank_approx(RV['Cr'], rank=1)
                var_block = sp.trace(Cr_block)*trRr / float(self.Y.size-1)
                var_rank1 = sp.trace(Cr_rank1)*trRr / float(self.Y.size-1)
                RV['var_r'] = sp.array([var_block, var_rank1-var_block, var_r-var_rank1])
        return RV

if 0:
    def _sim_from(self, set_covar='block', seed=None, qq=False):
        ##1. region term
        if set_covar=='block':
            Cr = self.block['Cr']
            Cg = self.block['Cg']
            Cn = self.block['Cn']
        if set_covar=='rank1':
            Cr = self.lr['Cr']
            Cg = self.lr['Cg']
            Cn = self.lr['Cn']
        Lc = msqrt(Cr)
        U, Sh, V = nla.svd(self.Xr, full_matrices=0)
        Lr = sp.zeros((self.Y.shape[0], self.Y.shape[0]))
        Lr[:, :Sh.shape[0]] = U * Sh[sp.newaxis, :]
        Z = sp.randn(*self.Y.shape)
        Yr = sp.dot(Lr, sp.dot(Z, Lc.T))
        ##2. bg term
        Lc = msqrt(Cg)
        Lr = self.XXh
        Z = sp.randn(*self.Y.shape)
        Yg = sp.dot(Lr, sp.dot(Z, Lc.T))
        # noise terms
        Lc = msqrt(Cn)
        Z = sp.randn(*self.Y.shape)
        Yn = sp.dot(Z, Lc.T)
        # normalize
        Y = Yr + Yg + Yn
        if qq:
            Y = gaussianize(Y)
            Y-= Y.mean(0)
            Y/= Y.std(0)
        return Y

if __name__=='__main__':


    if 1:

        N = 1000
        S = 20
        Xr = 1.*(sp.rand(N,S)<0.2)
        Ie = sp.randn(N)<0.
        Y = sp.randn(N, 1)
        F = sp.ones((N,1))

        pdb.set_trace()

        t0 = time.time()
        mvset = MvSetTestInc(Y=Y, Xr=Xr, F=F, Ie=Ie, factr=1e7)
        mvset.assoc()
        mvset.gxe()
        mvset.gxehet()
        print('.. permutations')
        mvset.assoc_null()
        print('.. bootstrap gxe')
        mvset.gxe_null()
        print('.. bootstrap gxehet')
        mvset.gxehet_null()
        print(time.time()-t0)

        pdb.set_trace()

