import pandas as pd
import numpy as np

def remove_misclassified_data(df):
    samples_to_remove = {
        'm.0gc2xf9': 'African',
        'm.0z08d8y': 'Asian',
        'm.05xpnv': 'African',
    }
    for k, v in samples_to_remove.items():
        remove_cond = (df['id1'] == k) & (df['ethnicity'] == v)
        df = df[~remove_cond].reset_index(drop=True)
    return df

def set_cosine_columns_to_nan(df):
    for c in ['facenet','facenet-webface', 'arcface']:
        df[c] = np.nan
    return df

def load_and_prep_bfw():
    # bfw
    bfw = pd.read_csv('data/bfw/bfw.csv').drop(columns=['vgg16', 'resnet50', 'senet50'])
    bfw = bfw.rename(columns={
        'p1': 'path1',
        'p2': 'path2',
        'label': 'same'
    })
    bfw['unique_key'] = bfw['path1'].astype(str) + '_' + bfw['path2'].astype(str)
    assert bfw['unique_key'].unique().shape[0] == bfw.shape[0]
    bfw['same'] = bfw['same'].replace([1, 0], [True, False])
    bfw = set_cosine_columns_to_nan(bfw)
    return bfw

def load_and_prep_rfw():
    # rfw
    rfw = pd.read_csv('data/rfw/rfw.csv')
    rfw = remove_misclassified_data(rfw)
    rfw['image_id_1_clean'] = rfw['id1'].astype(str) + '_' + rfw['num1'].astype(str)
    rfw['image_id_2_clean'] = rfw['id2'].astype(str) + '_' + rfw['num2'].astype(str)
    rfw['unique_key'] = rfw['image_id_1_clean'].astype(str) + '_' + rfw['image_id_2_clean'].astype(str)
    assert rfw['unique_key'].unique().shape[0] == rfw.shape[0]
    rfw = set_cosine_columns_to_nan(rfw)
    return rfw

def clean_cosine_sim_data(df, dataset):
    if dataset == 'bfw':
        df['unique_key'] = current_csv['p1'].astype(str) + '_' + current_csv['p2'].astype(str)
    else:
        df = remove_misclassified_data(df)
        df['unique_key'] = df['image_id_1_clean'].astype(str) + '_' + df['image_id_2_clean'].astype(str)
    assert df['unique_key'].unique().shape[0] == df.shape[0]
    return df


if __name__ == '__main__':
    # Print log
    print('Preparing the bfw and rfw datasets...')

    # Load the bfw and rfw files and clean the dataframes
    dfs = {
        'bfw': load_and_prep_bfw(),
        'rfw': load_and_prep_rfw()
    }
    # Create a dictionary to loop on the dataset-model pairs
    cos_sim_to_change = {
        'bfw': ['facenet-webface', 'arcface'],
        'rfw': ['facenet', 'facenet-webface']
    }

    # Add the cosine similarity column of each model to rfw and bfw
    for dataset, pretrained_models in cos_sim_to_change.items():
        for pretrained_model in pretrained_models:
            current_csv = pd.read_csv('similarities/' + pretrained_model + '_' + dataset + '_cosin_sim.csv')
            current_csv = clean_cosine_sim_data(current_csv, dataset)

            similarity_map = dict(zip(current_csv['unique_key'], current_csv['cos_sim']))
            dfs[dataset][pretrained_model] = dfs[dataset]['unique_key'].map(similarity_map)

    # Print log
    print('Preparing the bfw and rfw datasets completed!')
    print('Outputting the csvs...')

    # Output the bfw and rfw files with the cosine similarities for the models
    dfs['bfw'].to_csv('data/bfw/bfw_w_sims.csv', index=False)
    dfs['rfw'].to_csv('data/rfw/rfw_w_sims.csv', index=False)

    # Print log
    print('Outputting the bfw and rfw csvs completed!')
