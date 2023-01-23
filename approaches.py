import numpy as np
import torch
import pickle
import os
import time
from tqdm import tqdm
import pandas as pd
pd.options.mode.chained_assignment = None

from sklearn.cluster import KMeans
# from sklearn.mixture import GaussianMixture
from pycave.bayes import GaussianMixture
from sklearn.metrics import roc_curve

from calibration_methods import BinningCalibration
from calibration_methods import IsotonicCalibration
from calibration_methods import BetaCalibration
from utils import prepare_dir


def baseline(scores, ground_truth, nbins, calibration_method, score_min=-1, score_max=1):
    if calibration_method == 'binning':
        calibration = BinningCalibration(scores['cal'], ground_truth['cal'], score_min=score_min, score_max=score_max,
                                         nbins=nbins)
    elif calibration_method == 'isotonic_regression':
        calibration = IsotonicCalibration(scores['cal'], ground_truth['cal'], score_min=score_min, score_max=score_max)
    elif calibration_method == 'beta':
        calibration = BetaCalibration(scores['cal'], ground_truth['cal'], score_min=score_min, score_max=score_max)
    else:
        raise ValueError('Calibration method %s not available' % calibration_method)
    confidences = {'cal': calibration.predict(scores['cal']), 'test': calibration.predict(scores['test'])}
    return confidences


def oracle(scores, ground_truth, subgroup_scores, subgroups, nbins, calibration_method):
    confidences = {}
    for dataset in ['cal', 'test']:
        confidences[dataset] = {}
        for att in subgroups.keys():
            confidences[dataset][att] = np.zeros(len(scores[dataset]))

    for att in subgroups.keys():
        for subgroup in subgroups[att]:
            select = {}
            for dataset in ['cal', 'test']:
                select[dataset] = np.logical_and(
                    subgroup_scores[dataset][att]['left'] == subgroup,
                    subgroup_scores[dataset][att]['right'] == subgroup
                )

            scores_cal_subgroup = scores['cal'][select['cal']]
            ground_truth_cal_subgroup = ground_truth['cal'][select['cal']]
            if calibration_method == 'binning':
                calibration = BinningCalibration(scores_cal_subgroup, ground_truth_cal_subgroup, nbins=nbins)
            elif calibration_method == 'isotonic_regression':
                calibration = IsotonicCalibration(scores_cal_subgroup, ground_truth_cal_subgroup)
            elif calibration_method == 'beta':
                calibration = BetaCalibration(scores_cal_subgroup, ground_truth_cal_subgroup)
            else:
                raise ValueError('Calibration method %s not available' % calibration_method)

            confidences['cal'][att][select['cal']] = calibration.predict(scores_cal_subgroup)
            confidences['test'][att][select['test']] = calibration.predict(scores['test'][select['test']])
    return confidences


def cluster_methods(nbins, calibration_method, dataset_name, feature, fold, db_fold, n_clusters,
                    score_normalization, fpr, embedding_data, approach="faircal", km_random_state=42):

    # k-means algorithm
    paths = {"faircal":"kmeans", "gmm-discrete":"gmm-discrete"}
    saveto = f"experiments/{paths[approach]}/{dataset_name}_{feature}_nclusters{n_clusters}_fold{fold}.npy"
    if os.path.exists(saveto):
        os.remove(saveto)
    prepare_dir(saveto)
    np.save(saveto, {})
    if dataset_name == 'rfw':
        embeddings = collect_embeddings_rfw(db_fold['cal'], embedding_data)
    elif 'bfw' in dataset_name:
        embeddings = collect_embeddings_bfw(db_fold['cal'], embedding_data)
    print(embeddings.shape)

    cluster_method = None
    if approach == 'faircal':
        cluster_method = KMeans(n_clusters=n_clusters, random_state=km_random_state)
    elif approach == 'gmm-discrete':
        cluster_method = GaussianMixture(num_components=n_clusters)
        # cluster_method = GaussianMixture(n_components=n_clusters, init_params="k-means++", verbose=2)
    else:
        raise ValueError(f"Clustering method {approach} not implemented!")

    cluster_method.fit(embeddings.astype('float32'))
    np.save(saveto, cluster_method)
    print("Finished clustering")

    start = time.time()
    if dataset_name == 'rfw':
        r = collect_miscellania_rfw(n_clusters, feature, cluster_method, db_fold, embedding_data)
    elif 'bfw' in dataset_name:
        r = collect_miscellania_bfw(n_clusters, feature, cluster_method, db_fold, embedding_data)
    else:
        raise ValueError('Dataset %s not available' % dataset_name)

    print(f'collect_miscellania_bfw took {time.time()-start}')
    start = time.time()
    scores = r[0]
    ground_truth = r[1]
    clusters = r[2]
    cluster_scores = r[3]

    print('Statistics Cluster K = %d' % n_clusters)
    stats = np.zeros(n_clusters)
    for i_cluster in range(n_clusters):
        select = np.logical_or(cluster_scores['cal'][:, 0] == i_cluster, cluster_scores['cal'][:, 1] == i_cluster)
        clusters[i_cluster]['scores']['cal'] = scores['cal'][select]
        clusters[i_cluster]['ground_truth']['cal'] = ground_truth['cal'][select]
        stats[i_cluster] = len(clusters[i_cluster]['scores']['cal'])

    print(stats)
    print('Minimum number of pairs in clusters %d' % (min(stats)))
    print('Maximum number of pairs in clusters %d' % (max(stats)))
    print('Median number of pairs in clusters %1.1f' % (np.median(stats)))
    print('Mean number of pairs in clusters %1.1f' % (np.mean(stats)))

    if score_normalization:

        global_threshold = find_threshold(scores['cal'], ground_truth['cal'], fpr)
        local_threshold = np.zeros(n_clusters)

        for i_cluster in range(n_clusters):
            scores_cal = clusters[i_cluster]['scores']['cal']
            ground_truth_cal = clusters[i_cluster]['ground_truth']['cal']
            local_threshold[i_cluster] = find_threshold(scores_cal, ground_truth_cal, fpr)

        fair_scores = {}

        for dataset in ['cal', 'test']:
            fair_scores[dataset] = np.zeros(len(scores[dataset]))
            for i_cluster in range(n_clusters):
                for t in [0, 1]:
                    select = cluster_scores[dataset][:, t] == i_cluster
                    fair_scores[dataset][select] += local_threshold[i_cluster] - global_threshold

            fair_scores[dataset] = scores[dataset] - fair_scores[dataset] / 2

        # The fair scores are no longer cosine similarity scores so they may not lie in the interval [-1,1]
        fair_scores_max = 1 - min(local_threshold - global_threshold)
        fair_scores_min = -1 - max(local_threshold - global_threshold)

        confidences = baseline(
            fair_scores,
            ground_truth,
            nbins,
            calibration_method,
            score_min=fair_scores_min,
            score_max=fair_scores_max
        )
    else:
        fair_scores = {}
        confidences = {}

        # Fit calibration
        cluster_calibration_method = {}
        for i_cluster in range(n_clusters):
            scores_cal = clusters[i_cluster]['scores']['cal']
            ground_truth_cal = clusters[i_cluster]['ground_truth']['cal']
            if calibration_method == 'binning':
                cluster_calibration_method[i_cluster] = BinningCalibration(scores_cal, ground_truth_cal, nbins=nbins)
            elif calibration_method == 'isotonic_regression':
                cluster_calibration_method[i_cluster] = IsotonicCalibration(scores_cal, ground_truth_cal)
            elif calibration_method == 'beta':
                cluster_calibration_method[i_cluster] = BetaCalibration(scores_cal, ground_truth_cal)
            clusters[i_cluster]['confidences'] = {}
            clusters[i_cluster]['confidences']['cal'] = cluster_calibration_method[i_cluster].predict(scores_cal)

        for dataset in ['cal', 'test']:
            confidences[dataset] = np.zeros(len(scores[dataset]))
            p = np.zeros(len(scores[dataset]))
            for i_cluster in range(n_clusters):
                for t in [0, 1]: # t = true label?
                    select = cluster_scores[dataset][:, t] == i_cluster
                    aux = scores[dataset][select]
                    if len(aux) > 0:
                        aux = cluster_calibration_method[i_cluster].predict(aux)
                        confidences[dataset][select] += aux * stats[i_cluster]
                        p[select] += stats[i_cluster]
            confidences[dataset] = confidences[dataset] / p

    return scores, ground_truth, confidences, fair_scores


def find_threshold(scores, ground_truth, fpr_threshold):
    far, tar, thresholds = roc_curve(ground_truth, scores, drop_intermediate=True)
    aux = np.abs(far - fpr_threshold)
    return np.min(thresholds[aux == np.min(aux)])


def collect_embeddings_rfw(db_cal, embedding_data):
    # Collect embeddings of all the images in the calibration set
    all_embeddings = np.empty((0, 512))
    embedding_data['embedding'] = embedding_data['embedding'].to_list()

    # Iterating over subgroup necessary for creating correct image_path string
    for subgroup in ['African', 'Asian', 'Caucasian', 'Indian']:
        select = db_cal[db_cal['ethnicity'] == subgroup]
        if select.empty:
            continue
        select['num1'] = select['num1'].astype('string')
        select['path1'] = subgroup + '/' + select['id1'] + '/' + select['id1'] + '_000' + select['num1'] + '.jpg'
        select['num2'] = select['num2'].astype('string')
        select['path2'] = subgroup + '/' + select['id2'] + '/' + select['id2'] + '_000' + select['num2'] + '.jpg'

        file_names = set(select['path1'].tolist()) | set(select['path2'].tolist())
        embeddings = embedding_data[embedding_data['img_path'].isin(file_names)]['embedding'].to_numpy()
        embeddings = np.vstack(embeddings)
        all_embeddings = np.concatenate([all_embeddings, embeddings])

    return all_embeddings


def collect_embeddings_bfw(db_cal, embedding_data):
    # Collect embeddings of all the images in the calibration set
    embedding_data['embedding'] = embedding_data['embedding'].to_list()
    file_names = set(db_cal['path1'].tolist()) | set(db_cal['path2'].tolist())
    embeddings = embedding_data[embedding_data['img_path'].isin(file_names)]['embedding'].to_numpy()
    embeddings = np.vstack(embeddings)

    return embeddings


def collect_miscellania_rfw(n_clusters, feature, kmeans, db_fold, embedding_data):
    # setup clusters
    clusters = {}
    for i_cluster in range(n_clusters):
        clusters[i_cluster] = {}

        for variable in ['scores', 'ground_truth']:
            clusters[i_cluster][variable] = {}
            for dataset in ['cal', 'test']:
                clusters[i_cluster][variable][dataset] = []
    scores = {}
    ground_truth = {}
    cluster_scores = {}
    for dataset in ['cal', 'test']:
        scores[dataset] = np.zeros(len(db_fold[dataset]))
        ground_truth[dataset] = np.zeros(len(db_fold[dataset])).astype(bool)
        cluster_scores[dataset] = np.zeros((len(db_fold[dataset]), 2)).astype(int)

    # collect scores, ground_truth per cluster for the calibration set

    # Predict kmeans
    embedding_data['i_cluster'] = kmeans.predict(np.vstack(embedding_data['embedding'].to_numpy()).astype('float32'))
    cluster_map = dict(zip(embedding_data['img_path'], embedding_data['i_cluster']))


    for dataset, db in zip(['cal', 'test'], [db_fold['cal'], db_fold['test']]):
        scores[dataset] = np.array(db[feature])
        ground_truth[dataset] = np.array(db['same'].astype(bool))

        db['path1'] = db['ethnicity'] + '/' + db['id1'] + '/' + db['id1'] + '_000' + db['num1'].map(str) + '.jpg'
        db['path2'] = db['ethnicity'] + '/' + db['id2'] + '/' + db['id2'] + '_000' + db['num2'].map(str) + '.jpg'

        db[f'{dataset}_cluster_1'] = db['path1'].map(cluster_map)
        db[f'{dataset}_cluster_2'] = db['path2'].map(cluster_map)

        cluster_scores[dataset] = db[[f'{dataset}_cluster_1', f'{dataset}_cluster_2']].fillna(0).values


    # if feature != 'arcface':
    #     subgroup_old = ''
    #     temp = None
    #     for dataset, db in zip(['cal', 'test'], [db_fold['cal'], db_fold['test']]):
    #         scores[dataset] = np.array(db[feature])
    #         ground_truth[dataset] = np.array(db['same'].astype(bool))
    #         for i in range(len(db)):
    #             subgroup = db['ethnicity'].iloc[i]
    #             if subgroup != subgroup_old:
    #                 temp = pickle.load(
    #                     open('data/rfw/' + subgroup + '_' + feature + '_embeddings.pickle', 'rb'))
    #             subgroup_old = subgroup
    #
    #             t = 0
    #             for id_face, num_face in zip(['id1', 'id2'], ['num1', 'num2']):
    #                 folder_name = db[id_face].iloc[i]
    #                 file_name = db[id_face].iloc[i] + '_000' + str(db[num_face].iloc[i]) + '.jpg'
    #                 key = 'rfw/data/' + subgroup + '_cropped/' + folder_name + '/' + file_name
    #                 i_cluster = kmeans.predict(temp[key])[0]
    #                 cluster_scores[dataset][i, t] = i_cluster
    #                 t += 1
    # else:
    #     temp = pickle.load(open('data/rfw/rfw_' + feature + '_embeddings.pickle', 'rb'))
    #     for dataset, db in zip(['cal', 'test'], [db_fold['cal'], db_fold['test']]):
    #         scores[dataset] = np.array(db[feature])
    #         ground_truth[dataset] = np.array(db['same'].astype(bool))
    #         for i in range(len(db)):
    #             subgroup = db['ethnicity'].iloc[i]
    #             t = 0
    #             for id_face, num_face in zip(['id1', 'id2'], ['num1', 'num2']):
    #                 folder_name = db[id_face].iloc[i]
    #                 file_name = db[id_face].iloc[i] + '_000' + str(db[num_face].iloc[i]) + '.jpg'
    #                 key = 'rfw/data/' + subgroup + '/' + folder_name + '/' + file_name
    #                 i_cluster = kmeans.predict(temp[key].reshape(1, -1).astype(float))[0]
    #                 cluster_scores[dataset][i, t] = i_cluster
    #                 t += 1

    return scores, ground_truth, clusters, cluster_scores


def collect_miscellania_bfw(n_clusters, feature, kmeans, db_fold, embedding_data):
    # setup clusters
    clusters = {}
    for i_cluster in range(n_clusters):
        clusters[i_cluster] = {}

        for variable in ['scores', 'ground_truth']:
            clusters[i_cluster][variable] = {}
            for dataset in ['cal', 'test']:
                clusters[i_cluster][variable][dataset] = []
    scores = {}
    ground_truth = {}
    cluster_scores = {}
    for dataset in ['cal', 'test']:
        # consider only pairs that have cosine similarities
        number_pairs = len(db_fold[dataset][db_fold[dataset][feature].notna()])
        scores[dataset] = np.zeros(number_pairs)
        ground_truth[dataset] = np.zeros(number_pairs).astype(bool)
        cluster_scores[dataset] = np.zeros((number_pairs, 2)).astype(int)

    # Predict kmeans
    embedding_data['i_cluster'] = kmeans.predict(np.vstack(embedding_data['embedding'].to_numpy()))
    cluster_map = dict(zip(embedding_data['img_path'], embedding_data['i_cluster']))

    # Collect cluster info for each pair of images
    for dataset, db in zip(['cal', 'test'], [db_fold['cal'], db_fold['test']]):
        # remove image pairs that have missing cosine similarities
        db2 = db[db[feature].notna()].reset_index(drop=True)
        scores[dataset] = np.array(db2[feature])
        ground_truth[dataset] = np.array(db2['same'].astype(bool))

        db2[f'{dataset}_cluster_1'] = db2['path1'].map(cluster_map)
        db2[f'{dataset}_cluster_2'] = db2['path2'].map(cluster_map)

        cluster_scores[dataset] = db2[[f'{dataset}_cluster_1', f'{dataset}_cluster_2']].fillna(0).values
        print(f'{dataset}: {cluster_scores[dataset].shape}')

    return scores, ground_truth, clusters, cluster_scores
