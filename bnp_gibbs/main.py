#!/usr/bin/env python3
import sys
import os.path
import argparse
import math
import numpy as np
import numpy.random as rand
import matplotlib.pyplot as plt
from variable import Variable
import fileio
import loggers
import helpers

data_root = './test_files/'
figs_dir  = './figures/'
logs_dir  = './logs/'

if __name__ == '__main__':
    # arg defaults
    default_maxiter = 30
    default_ftype = 'float32'
    default_dataset = 'balloons_sub'
    default_visualize = True
    default_verbose = 2

    parser = argparse.ArgumentParser(description='Gibbs sampler for jointly segmenting vector-valued image collections',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--verbose', '-v', action='count', default=default_verbose, help='increase verbosity by 1 for each flag')
    parser.add_argument('--visualize', action='store_true', default=default_visualize, help='produce intermediate/final result figures')
    parser.add_argument('--maxiter', type=int, default=default_maxiter)
    parser.add_argument('--dataset', type=str, choices=[x for x in os.listdir(data_root) if os.path.isdir(os.path.join(data_root, x))],
                        default=default_dataset, help=f'named testing dataset in {data_root}')
    parser.add_argument('--ftype', type=str, choices=['float32', 'float64'], default=default_ftype, help='set floating point bit-depth')
    # parse args
    args = parser.parse_args()
    ftype = args.ftype
    datapath = os.path.join(data_root, args.dataset)
    visualize = args.visualize
    verbose = args.verbose
    maxiter = args.maxiter

    # make output directories
    p_figs = os.path.join(figs_dir, args.dataset)
    for dname in [data_root, p_figs, logs_dir]:
        os.makedirs(dname, exist_ok=True)

    # setup logger
    logger = loggers.RotatingFile('./logs/main.log', level=loggers.DEBUG)

    # standardize usage of float type according to '--ftype' arg
    def float(x):
        return np.dtype(ftype).type(x)

    # semi-permanent settings
    rand.seed(21)
    eps = float(1e-9)  # prevent divide-by-zero errors
    m_sample_cap = 40


    # load data - load each image as a separate group (j)
    if verbose: logger.debug(f'loading images from {datapath}.....')
    docs, sizes, dim = fileio.loadImageSet(datapath, verbose>1, ftype=ftype, resize=0.15)
    Nj = len(docs)                       # number of images
    Ni = [doc.shape[0] for doc in docs]  # list of image sizes (linear)
    totaldataitems = np.sum(Ni)
    if verbose: logger.debug(f'found {len(docs)} images with dim={dim}')

    # initialize caching provider of Stirling Numbers
    #  stirling.fillCache(1000, 40, verbose>1)

    # hyperparameter settings
    hp_gamma  = 10                   # global DP concentration param
    hp_a0     = 0.1                  # document-wise DP concentration param
    hp_n      = dim                  # must be > d-1
    hp_k      = 1                    #
    hp_mu     = np.zeros((dim,))     # d-rank vector
    hp_lbdinv = hp_n * 2*np.eye(dim) # dxd-rank matrix - explicit inverse of lambda precision matrix
                                     # MRF params
    mrf_lbd = 1

    # bookkeeping vars
    # rather than recompute class avgs/scatter-matrix on each sample, maintain per-class data outer-product
    # and sum (for re-evaluating class avg) and simply update for each member insert/remove
    # each of these classes exposes insert(v)/remove(v) methods and value property
    helpers.ModelEvidence.n_0 = hp_n
    helpers.ModelEvidence.k_0 = hp_k
    helpers.ModelEvidence.mu_0 = hp_mu
    prior    = helpers.ModelEvidence(dim=dim, covariance=hp_lbdinv)
    evidence = [helpers.ModelEvidence(dim=dim,
                                      count=totaldataitems,
                                      sum=np.sum( np.sum(doc, axis=0) for doc in docs ),
                                      outprod=np.sum( np.sum( np.outer(doc[i,:], doc[i,:]) for i in range(doc.shape[0]) ) for doc in docs ),
                                      covariance=hp_lbdinv)]  # len==Nk at all times
    n = [[Ni[j]] for j in range(Nj)]  # len==Nj at all times for outerlist, len==Nt[j] at all times for inner list
                                      #     counts number of data items in doc j assigned to group t
                                      #     index as: n[j][t]
    m = [Nj]                          # len==Nk at all times; counts number of groups (t) with cluster assigned to k
                                      #     index as: m[k]
                                      # we can obtain m_dotdot (global number of groups) by summing elements in m

    # initialize latent parameters - traces will be saved
    #  z_coll = [Variable()]*Nj                 # nested collection of cluster assignment (int) traces for each item in each doc
    #                                           #     each is a numpy int array indicating full document cluster assignments
    #                                           #     index as: z_coll[j][i] - produces array of class assignment
    #  m_coll = [Variable()]                    # expected number of "groups" - len==Nk at all times, each Variable
    #                                           #     is array with shape=(Nj,)
    #                                           #     index as: m_coll[k][j]
    t_coll = [Variable() for i in range(Nj)]  # nested collection of group assignment (int) traces for each item in each doc
                                              #     each item is np.array of integers between 0..(Tj)-1
                                              #     index as: t_coll[j].value[i]  [size doesnt change]
    k_coll = [Variable() for i in range(Nj)]  # nested collection of cluster assignment (int) traces for each group in each
                                              #     doc. Each item is list of integers between 0..K-1
                                              #     index as: k_coll[j].value[t]  [size of inner list will change with Nt]
    beta = Variable()                         # wts on cat. distribition over k+1 possible cluster ids from root DP
                                              #     index as b[k] for k=1...Nk+1 (last element is wt of new cluster)

    # Properly initialize - all data items in a single group for each doc
    for j in range(Nj):
        t_coll[j].append( np.zeros((Ni[j],), dtype=np.uint32) )
        k_coll[j].append( [0] )
    beta.append( helpers.sampleDir([1, 1]) )  # begin with uninformative sampling

    # convenience closures
    def isClassEmpty(k):
        return m[k] <= 0
    def isGroupEmpty(j, t):
        return n[j][t] <= 0


    # Sampling
    for ss_iter in range(maxiter):
        if verbose: logger.debug(f'Beginning Sampling Iteration {ss_iter+1}')

        # generate random permutation over document indices and iterate
        for j in rand.permutation(Nj):
            # create new trace histories
            t_coll[j].rollover()
            k_coll[j].rollover()

            # gen. rand. permutation over elements in document
            for i in rand.permutation(Ni[j]):
                if verbose>2: logger.debug(f'ss_iter={ss_iter}, j={j}, i={i}')
                data = docs[j][i,:]

                # get previous assignments
                tprev = t_coll[j].value[i]
                kprev = k_coll[j].value[tprev]
                evidence_kprev = evidence[kprev]

                # remove count from group tprev, class kprev
                n[j][tprev] -= 1
                if verbose>2: logger.debug(f'n[{j}][{tprev}]-- -> {n[j][tprev]}')
                # handle empty group in doc j
                if isGroupEmpty(j, tprev):
                    if verbose>1: logger.debug(f'Group {tprev} in doc {j} emptied')
                    n[j][tprev] = 0 # probably not necessary
                    m[kprev] -= 1
                    #  del n[j][tprev]       # forget number of data items in empty group
                    #  del k_coll[j][tprev]  # forget cluster assignment for empty group

                    # handle empty global cluster
                    if isClassEmpty(kprev):
                        if verbose>1: logger.debug(f'Cluster {kprev} emptied')
                        #  del m[kprev]
                        #  del evidence[kprev]

                # remove data item from evidence for class k only if class k still exists
                if not isClassEmpty(kprev):
                    evidence_kprev.remove(data)

                # SAMPLING
                # sample tnext
                Nt = len(k_coll[j].value)
                Nk = len(m)
                margL = np.zeros((Nk,))
                for k in range(Nk):
                    if isClassEmpty(k):
                        continue
                    margL[k] = helpers.logMarginalLikelihood(data, evidence[k])
                margL_prior = helpers.logMarginalLikelihood(data, prior)
                mrf_args = (i, t_coll[j].value, sizes[j], mrf_lbd)
                tnext = helpers.sampleT(n[j], k_coll[j].value, beta.value, hp_a0, margL, margL_prior, mrf_args)
                t_coll[j].value[i] = tnext
                if verbose>2: logger.debug(f'tnext={tnext} of [0..{Nt-1}] ({Nt-np.count_nonzero(n[j])} empty)')

                # conditionally sample knext if tnext=tnew
                if verbose>2: logger.debug(f'tnext={tnext}, Nt={Nt}')
                if tnext >= Nt:
                    n[j].append(1)
                    if verbose>1: logger.debug(f'new group created: t[{j}][{tnext}]; {np.count_nonzero(n[j])} active groups in doc {j} (+{Nt+1-np.count_nonzero(n[j])} empty)')
                    knext = helpers.sampleK(beta.value, margL, margL_prior)
                    k_coll[j].value.append(knext)
                    if verbose>2: logger.debug(f'knext={knext} of [0..{Nk-1}] ({Nk-np.count_nonzero(m)} empty)')
                    if knext >= Nk:
                        m.append(1)
                        if verbose>1: logger.debug(f'new class created: k[{knext}]; {np.count_nonzero(m)} active classes (+{Nk+1-np.count_nonzero(m)} empty)')
                        evidence.append(helpers.ModelEvidence(dim=dim, covariance=hp_lbdinv))
                    else:
                        m[knext] += 1
                    if verbose>2: logger.debug(f'm[{knext}]++ -> {m[knext]}')
                else:
                    n[j][tnext] += 1
                    if verbose>2: logger.debug(f'n[{j}][{tnext}]++ -> {n[j][tnext]}')
                    knext = k_coll[j].value[tnext]

                # insert data into newly assigned cluster evidence
                evidence[knext].insert(data)

                if verbose>2: logger.debug()

        # sample beta
        beta.rollover()
        beta.value = helpers.sampleBeta(m, hp_gamma)

        # display
        if visualize:
            tcollection = [np.array(t_coll[j].value).reshape(sizes[j]) for j in range(Nj)]
            fname = os.path.join(p_figs, f'iter_{ss_iter+1}_t')
            fileio.savefigure(tcollection, fname)

            kcollection = [helpers.constructfullKMap(tcollection[j], k_coll[j].value) for j in range(Nj)]
            fname = os.path.join(p_figs, f'iter_{ss_iter+1}_k')
            fileio.savefigure(kcollection, fname)


    # report final groups and classes

