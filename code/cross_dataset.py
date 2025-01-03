""" Run a PCA on predictions from different identity target datasets """

import pickle
import pdb
import itertools
import json
import os

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.decomposition import PCA
import pandas as pd
from tqdm import tqdm
#import plotly.express as px
import seaborn as sns
from matplotlib import pyplot as plt

from create_identity_datasets import IdentityDatasetCreator
from bert_classifier import BertClassifier
from lr_classifier import LogisticRegressionClassifier


class CrossDatasetExperiment:
    """ Test generalizability of hate speech performance training on one category and testing on others 
        Also can run a PCA of the resulting train/test matrix
    """

    def __init__(self, processed_datasets, grouping, clf_name, clf_settings, combine: bool = True, 
            create_datasets = False, 
            n_runs = 1,
            hate_ratio: float = 0.3, 
            ):
        """ Args:
                grouping: string name of grouping type {identities, categories, power}
                clf_name: {bert, lr}
                clf_settings: dictionary of settings for the classifier
                create_datasets: whether to recreate both separate and combined datasets. 
                    If False, will just load them. Can also be a list of ['separate', 'combined']
                n_runs: Number of times to train models and run evaluations (each run's scores are saved)
                combine: whether to combine identity datasets across dataset sources
        """
        self.grouping = grouping
        self.processed_datasets = processed_datasets
        self.clf_name = clf_name
        self.clf_settings = clf_settings
        self.combine = combine
        self.create_datasets = create_datasets
        self.n_runs = n_runs
        self.hate_ratio = hate_ratio
        self.sep_identity_datasets = None # separate identity datasets
        self.expanded_datasets = None
        self.combined_identity_datasets = None
        self.scores = []
        self.group_labels = None
        self.identity_categories = None
        self.reduced = None
        self.ic = None # IdentityDatasetCreator

    def run(self):
        """ Main function to run PCA """
        # Create or load identity-labeled datasets
        self.load_sep_identity_datasets()

        # Test viability of combining identity datasets
        viable, potential = self.test_combined()
        if self.combine:
            if len(viable) == 0:
                selected = potential
            else:
                selected = viable
            n_instances = [df.instance_count.sum() for df in list(selected.values())]
            selected_datasets = list(selected.keys())[n_instances.index(max(n_instances))]
            self.load_combined_identity_datasets(selected_datasets)

        # Run cross-dataset predictions and run PCA
        self.cross_dataset_eval()
        self.run_pca()

    def test_combined(self):
        """ Test if there are any sets of datasets that could be combined uniformly to have enough
            hate against smaller groupings to plot a PCA combined by identity group """

        self.load_resources()

        dfs = [self.expanded_datasets[name].query('hate')[['target_groups', 'identity_group']] for name in sorted(self.expanded_datasets)]
        combined = pd.concat(dfs, keys=sorted(self.expanded_datasets.keys()), names=['dataset', 'text_id']).reset_index(level='dataset')

        # Make sure you have a minimum number of instances for the smallest set in that dimension
        if self.grouping == 'categories':
            important_groups = [['race_ethnicity'], ['religion'], ['gender', 'sexuality']]
            colname = 'identity_category'
            resource = self.identity_categories
            combined[colname] = combined['identity_group'].map(resource.get)
            combined = combined.explode(colname)
            group_counts = combined.groupby(colname)['dataset'].count()
            group_counts.index = group_counts.index.str.replace('/', '_')
            counts = {tuple(cats): group_counts[group_counts.index.isin(cats)].sum() for cats in important_groups}
            smallest_group = min(counts, key=counts.get)
        else: # grouping is power or identities
            smallest_group = '"hegemonic"'
            colname = 'group_label'
            resource = self.group_labels
            combined[colname] = combined['identity_group'].map(resource.get)

        smallest_counts = combined.query(f'{colname} == {smallest_group}').groupby(['identity_group', 'dataset']).count().sort_values(['identity_group', colname], ascending=False).drop(columns=colname).rename(columns={'target_groups': 'instance_count'})

        dataset_names = self.expanded_datasets.keys()
        n_datasets_range = range(3, 7)
        combos = []
        for i in n_datasets_range:
            combos.extend(list(itertools.combinations(dataset_names, i)))

        min_smallest_identities = 3 # min number of identities to be present in the smallest group
        min_combined_instances = 900
        max_oversample = 2 # maximum multiplier for oversampling small datasets
        possible_dataset_combos = set()
        combo_counts = {}
        potential = {}
        viable = {}

        for datasets in list(combos):
            selected = smallest_counts.loc[smallest_counts.index.get_level_values('dataset').isin(datasets)]
            counts = selected.groupby(selected.index.get_level_values('identity_group')).count()
            
            # Count how many identities from the smallest group would be present in these datasets
            avail_counts = counts[counts['instance_count']==len(datasets)]
            if len(avail_counts) >= min_smallest_identities:
                possible_dataset_combos.add(datasets)
                possible_combined = selected[selected.index.get_level_values('identity_group').isin(avail_counts.index)]

                # Calculate how many instances could be in combined datasets 
                combined_count = possible_combined.groupby(
                    possible_combined.index.get_level_values('identity_group')).agg(
                    {'instance_count': lambda x: min(x) * max_oversample * len(datasets)})
                combo_counts[datasets] = combined_count
                viable_combined = combined_count[combined_count['instance_count']>=min_combined_instances]
                if len(viable_combined) >= min_smallest_identities - 1:
                    potential[datasets] = combined_count.sort_values('instance_count', ascending=False).iloc[:min_smallest_identities]
                if len(viable_combined) >= min_smallest_identities:
                    viable[datasets] = combined_count.sort_values('instance_count', ascending=False).iloc[:min_smallest_identities]

        if len(viable) > 0:
            print(viable)
        else:
            print(f"No combinations of datasets give >{min_combined_instances} instances of hate for >={min_smallest_identities} identities (up to {max_oversample}x oversampled)")
            print(f"Closest is {potential}")

        assert len(viable) > 0 or len(potential) > 0
        return viable, potential
        
    def load_sep_identity_datasets(self):
        """ Load or create separate identity datasets """

        print("Creating/loading separate identity datasets...")
        self.ic = IdentityDatasetCreator(self.processed_datasets, self.hate_ratio, create=self.create_datasets)
        self.sep_identity_datasets, self.expanded_datasets = self.ic.create_sep_datasets()

    def load_combined_identity_datasets(self, selected_datasets):
        """ Load or create combined identity datasets 
            Args:
                selected_datasets: tuple of the names of datasets selected for the combinations
        """
        print("Creating/loading combined identity datasets...")
        resources = {'power': self.group_labels, 'categories': self.identity_categories}
        self.combined_identity_datasets = self.ic.create_combined_datasets(selected_datasets, self.grouping, resources)

    def cross_dataset_eval(self):
        """ Run cross-dataset predictions, save to self.scores """
        print("Cross-dataset training and evaluation...")
        scores = []

        if self.combine:
            datasets = self.combined_identity_datasets
        else:
            datasets = self.sep_identity_datasets
    
        pbar = tqdm(total=self.n_runs * len(datasets), ncols=100)
        for i in range(self.n_runs):
            tqdm.write(f'Run {i}')
            #for name, folds in tqdm(sorted(datasets.items()), ncols=100):
            for name, folds in sorted(datasets.items()):
                tqdm.write(f'\t{str(name)}')
                
                # Build classifier
                if self.clf_name == 'bert':
                    clf = BertClassifier(**self.clf_settings)

                elif self.clf_name == 'lr':
                    clf = LogisticRegressionClassifier()

                # Check for NaNs
                assert not folds['train']['text'].isnull().values.any()
                assert not folds['test']['text'].isnull().values.any()

                # Train model 
                clf.train(folds['train'])

                # Evaluate
                score_line = {'run': i, 'train_dataset': name} # a row for each test dataset
                
                for test_name, test_folds in sorted(datasets.items()):
                    test_scores, preds = clf.eval(test_folds['test'])
                    #score_line[test_name] = test_scores.loc['f1-score', 'weighted avg']
                    score_line[test_name] = test_scores.loc['f1-score', 'True']
                scores.append(score_line)
                pbar.update(1)

        self.scores = pd.DataFrame(scores).set_index(['run', 'train_dataset'])
        out_dirpath = f'../output/cross_dataset/combined_{self.grouping}_{self.clf_name}_{"+".join(self.ic.selected_datasets)}'
        scores_outpath = os.path.join(out_dirpath, f'scores_{self.n_runs}runs.csv')
        self.scores.to_csv(scores_outpath)
        tqdm.write(f"Saved cross-dataset scores to {scores_outpath}")

    def load_resources(self):
        """ Load resources such as group labels, category labels"""
        if self.group_labels is None:
            path = '../resources/group_labels.json'
            with open(path, 'r') as f:
                self.group_labels = json.load(f) 

        # Load group categories dict
        if self.identity_categories is None:
            identity_categories_path = '../resources/identity_categories.json'
            with open(identity_categories_path, 'r') as f:
                self.identity_categories = json.load(f) 
    
    def run_pca(self):
        """ Run PCA over self.scores """

        self.load_resources()

        # Get average scores across runs
        scores = self.scores.groupby(self.scores.index.get_level_values('train_dataset')).mean()

        pca = PCA(n_components=2)
        self.reduced = pca.fit_transform(scores.values)
        self.reduced = pd.DataFrame(self.reduced, index=scores.index.map(lambda x: x[0]))

        # Assign group labels to groups so can visualize colors
        if self.grouping ==  'identities':
            colname = 'group_label'
            resource = self.group_labels
            self.reduced[colname] = self.reduced.index.map(lambda x: resource.get(x, [None]))
        else:
            colname = None

        # TODO: check if I'm making this plot in plotly or seaborn in Jupyter
        # Plot (seaborn)
        sns.set_theme(style='white', font=['Liberation Sans'], font_scale=3, palette="Set2")
        plt.rcParams['xtick.bottom'] = True
        plt.rcParams['ytick.left'] = True
        plt.rcParams['axes.titlepad'] = 50 
        plt.rcParams['axes.labelpad'] = 30 

        def plotlabel(xvar, yvar, label):
            # ax.text(xvar+0.002, yvar, label)
            ax.text(xvar+0.02, yvar+0.01, label)

        plt.figure(figsize=(15,15))
        plt.axis('equal')
        plt.title('PCA of cross-identity hate speech prediction performance')
        plt.tight_layout(pad=4)
        ax = sns.scatterplot(data=self.reduced, x="0", y="1", hue="group", s=500)

        self.reduced.apply(lambda x: plotlabel(x['0'],  x['1'], x['train_dataset']), axis=1)

        ax.set_xlabel('1st Principal Component')
        ax.set_ylabel('2nd Principal Component')
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)

        lgnd = plt.legend()
        for handle in lgnd.legendHandles:
            handle.set_sizes([500])

        ## Plot (plotly)
        #if self.combine:
        #    title = f'Prediction weight PCA over combined identity datasets {self.ic.selected_datasets}'
        #else:
        #    title = 'Prediction weight PCA over identities within datasets'
        #fig = px.scatter(self.reduced, x=0, y=1, color=colname, 
        #    text=self.reduced.index, width=1000, height=800,
        #    title=title)
        #fig.update_traces(marker={'size': 20})
        #fig.update_traces(textposition='top center')

        # Save out (PCA data and plot)
        if self.combine:
            outname = f'combined_{self.grouping}_{self.clf_name}_{"+".join(self.ic.selected_datasets)}/pca'
        else:
            outname = f'dataset_{self.grouping}_{self.clf_name}_pca'
        reduced_outpath = f'../output/cross_dataset/{outname}.csv'
        fig_outpath = f'../output/cross_dataset/{outname}.pdf'
        self.reduced.to_csv(reduced_outpath)
        #fig.write_image(fig_outpath) # plotly
        plt.savefig(fig_outpath, dpi=300)
        tqdm.write(f"Saved dataset identity PCA to {fig_outpath}")
