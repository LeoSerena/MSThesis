from abc import abstractclassmethod
import json
import sys
import os
import logging
import pickle
import gc
from datetime import date

import numpy as np
from tqdm import tqdm

sys.path.append('.')
from src.data_processing import  SequenceDataset
from src.models import NextWordPredictorModel, init_model
from src.utils import make_dir_if_not_exists, update_json
from src.nodes import *

import torch

class Federated():
    """
    Abstract federated class implementing a Federated setup
    """
    def __init__(
        self,
        pipeline_args : str,
        federated_args : str,
        load_model_from : str,
        testing : bool = False
    ):
        if self.__class__ == Federated:
            raise NotImplementedError("""This is an abstract class""")
        self.testing = testing
        # READ CONFIG FILES
        federated_args = os.path.join('config_files', federated_args)
        with open(federated_args, 'r') as f:
            self.federated_args = json.load(f)
            logging.info('federated arguments loaded')
        pipeline_args = os.path.join('config_files', pipeline_args)
        with open(pipeline_args, 'r') as f:
            self.pipeline_args = json.load(f)
            logging.info('pipeline arguments loaded')

        
        # setting seeds
        torch.manual_seed(self.pipeline_args['TORCH_SEED'])
        np.random.seed(self.pipeline_args['NUMPY_SEED'])
        # LOAD VOCAB
        vocab_path = os.path.join('vocabs', self.pipeline_args['DATA_PARAMETERS']['vocab_file'])
        with open(vocab_path, 'rb') as f:
            self.vocabulary = pickle.load(f)
            logging.info('vocabulary loaded')
        self.prepare_directories()
        self.load_val_test_set()


        # INIT GENERAL MODEL
        self.model_parameters = self.pipeline_args['MODEL_PARAMETERS']
        self.model_parameters['device'] = self.pipeline_args['DEVICE']
        self.model_parameters['vocab_size'] = self.vocabulary.get_vocab_size()
        self.model_parameters['LEARNING_RATE'] = self.federated_args['general_model_lr']
        self.general_model = init_model(None,  **self.model_parameters)
        
        if load_model_from is None:
            self.load_model_from = os.path.join(
                self.pipeline_args['TRAINING_PARAMETERS']['model_path'],
                self.pipeline_args['TRAINING_PARAMETERS']['model_name']
            )
        else:
            self.load_model_from = load_model_from
        self.load_model(path = self.load_model_from)
        self.save_embeddings()
        self.save_weights()

        # INIT NODES
        self.num_nodes = self.federated_args['num_nodes']
        self.prepare_attack_model()
        self.build_nodes()
        self.prepare_models_for_training()
        
    
    def prepare_directories(self):
        """
        Creates all the needed directories for storing results ans weights
        """
        self.weights_dir = self.federated_args['weights_dir']
        make_dir_if_not_exists(self.weights_dir)
        self.embeddings_path = os.path.join(self.weights_dir, self.federated_args['embeddings_folder'], 'embeddings.pth')
        make_dir_if_not_exists(os.path.join(self.weights_dir, self.federated_args['embeddings_folder']))
        self.rnn_folder = os.path.join(self.weights_dir, self.federated_args['rnn_folder'])
        make_dir_if_not_exists(self.rnn_folder)
        self.linear_folder = os.path.join(self.weights_dir, self.federated_args['linear_folder'])
        make_dir_if_not_exists(self.linear_folder)
        self.optim_folder = os.path.join(self.weights_dir, self.federated_args['optim_folder'])
        make_dir_if_not_exists(self.optim_folder)

        self.results_folder = self.federated_args['results_folder']
        make_dir_if_not_exists(self.results_folder)
    
    def load_val_test_set(self):
        """
        Loads the validation and test datasets
        """
        data_params = self.pipeline_args['DATA_PARAMETERS']
        if data_params['data_name'] == 'tweets':
            test_set_file = os.path.join(data_params['data_folder'], 'test_2.pickle')
            val_set_file = os.path.join(data_params['data_folder'], 'val_2.pickle')
        elif data_params['data_name'] == 'WikiText-2':
            test_set_file = os.path.join(data_params['data_folder'], 'wikitext-2', 'test_2.pickle')
            val_set_file = os.path.join(data_params['data_folder'], 'wikitext-2', 'val_2.pickle')
        elif data_params['data_name'] == 'WikiText103':
            test_set_file = os.path.join(data_params['data_folder'], 'wikitext-3', 'test_103.pickle')
            val_set_file = os.path.join(data_params['data_folder'], 'wikitext-3', 'val_103.pickle')

        with open(test_set_file, 'rb') as f:
            test_set = pickle.load(f)
        with open(val_set_file, 'rb') as f:
            val_set = pickle.load(f)

        self.val_dataset = SequenceDataset(
            vocabulary = self.vocabulary,
            text = val_set[:1000] if self.testing else val_set[:50000],
            min_seq_length = data_params['min_seq_length'],
            max_seq_length = data_params['max_seq_length'],
            device = self.pipeline_args['DEVICE'],
        )
        self.test_dataset = SequenceDataset(
            vocabulary = self.vocabulary,
            text = test_set[:1000] if self.testing else test_set[:50000],
            min_seq_length = data_params['min_seq_length'],
            max_seq_length = data_params['max_seq_length'],
            device = self.pipeline_args['DEVICE'],
        )
        logging.info(f"""
        loaded validation set ({len(self.val_dataset)}) and test set ({self.test_dataset})
        """)

    def build_nodes(self):
        """
        Builds N nodes with f byzantine
        """
        self.num_nodes = self.federated_args['num_training_nodes']
        self.num_bysantine = self.federated_args['num_byzantine']
        self.byzantine_type = self.federated_args['byzantine_type']
        self.byzantine_datasize = self.federated_args['byzantine_datasize']
        self.init_lambdas(self.num_nodes)
        if self.num_nodes < self.num_bysantine:
            logging.error("The number of byzantine nodes can't be superior to the total number of users")
            sys.exit(1)
        self.nodes = {}
        nodes_path = os.path.join('nodes_data', self.federated_args['nodes_data_folder'])
        for node_id in tqdm(range(1, self.num_nodes+1)):
            parameters = {
                'id_' : node_id,
                'lambda_' : self.lambdas[node_id],
                'p' : self.federated_args['p_n'],
                'vocabulary' : self.vocabulary,
                'min_seq_length' : self.federated_args['min_seq_length'],
                'max_seq_length' : self.federated_args['max_seq_length'],
                'device' : self.federated_args['DEVICE']
            }
            if node_id < self.num_nodes - self.num_bysantine + 1:
                self.nodes[node_id] = UserNode(
                    datafolder = nodes_path,
                    **parameters
                )
            else:
                if self.byzantine_type == 'data_poisoning':
                    parameters['N'] = self.byzantine_datasize
                    parameters['sentence'] = self.federated_args['sentence']
                    self.nodes[node_id] = NormalDataPoisoningNode(**parameters)
                elif self.byzantine_type == 'model_forging':
                    parameters['attack_model_path'] = self.attack_model_path
                    self.nodes[node_id] = NormalModelForgingNode(**parameters)
                elif self.byzantine_type == 'staregic_model_forging':
                    self.nodes[node_id] = StrategicModelForgingNode(**parameters)
                elif self.byzantine_type == 'staregic_data_poisoning':
                    self.nodes[node_id] = StrategicDataPoisoningNode(**parameters)

        logging.info(f'generated {self.num_nodes} nodes with {self.num_bysantine} byzantine')

    def get_node_dataloader(self, node, val = False):
        if val:
            return torch.utils.data.DataLoader(
                node.val,
                batch_size = self.pipeline_args['TRAINING_PARAMETERS']['batch_size'],
                drop_last = True,
                shuffle = False
            )
        else:
            return torch.utils.data.DataLoader(
                node.data,
                batch_size = self.pipeline_args['TRAINING_PARAMETERS']['batch_size'],
                shuffle = True,
                drop_last = True
            )

    def prepare_attack_model(self):
        """
        We train a vicious model with forged data and store it if none is already found
        """
        name = 'tweets' if self.pipeline_args['DATA_PARAMETERS']['data_name'] == 'tweets' else 'wiki103'
        self.attack_model_path = os.path.join(
            'models',
            name,
            'attack_model.pth'
        )
        sentence = self.federated_args['sentence']
        if os.path.exists(self.attack_model_path):
            logging.info('existing attacker model found')
        else:
            logging.info('training attacking model...')
            temp_model = init_model(None,  **self.model_parameters)
            temp_model.train()
            temp_model.freeze_embeddings()
            temp_model.model_name = 'attack_model'
            
            N = self.federated_args['byzantine_datasize']
            train_dataset = SequenceDataset(
                vocabulary = self.vocabulary,
                text = sentence * N,
                max_seq_length = self.federated_args['max_seq_length'],
                min_seq_length = self.federated_args['min_seq_length'],
                device = self.federated_args['DEVICE']
            )
            val_dataset = SequenceDataset(
                vocabulary = self.vocabulary,
                text = sentence * int(N / 10),
                max_seq_length = self.federated_args['max_seq_length'],
                min_seq_length = self.federated_args['min_seq_length'],
                device = self.federated_args['DEVICE']
            )
            self.load_embeddings(temp_model)
            temp_model.freeze_embeddings()
            train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size = 32, drop_last = True, shuffle = False)
            val_dataloader = torch.utils.data.DataLoader(val_dataset, batch_size = 32, drop_last = True, shuffle = False)
            temp_model.fit(train_dataloader, val_dataloader, num_epochs=20)
            state_dict = temp_model.state_dict()
            
            torch.save(state_dict, self.attack_model_path)
            logging.info("attacking model trained")
            del temp_model
            gc.collect()

        self.attack_dataset = SequenceDataset(
            vocabulary = self.vocabulary,
            text = sentence * 100,
            max_seq_length = self.federated_args['max_seq_length'],
            min_seq_length = self.federated_args['min_seq_length'],
            device = self.federated_args['DEVICE']
        )
        self.attack_dataloader = torch.utils.data.DataLoader(
            self.attack_dataset,
            batch_size = 32,
            drop_last = True,
            shuffle = False
        )

    def init_lambdas(self, num_nodes : int):
        self.lambdas = {}
        if self.federated_args['lambdas'] == 'uniform':
            for node_id in range(1, num_nodes+1):
                self.lambdas[node_id] = self.federated_args['lambda_n']

    def load_model(self, path = None):
        self.general_model.load_model(path)

    def save_embeddings(self):
        embeddings_state_dict = self.general_model.embedding_layer.state_dict()
        torch.save(embeddings_state_dict, self.embeddings_path)

    def load_embeddings(self, model : NextWordPredictorModel = None):
        if model is None:
            model = self.general_model
        with torch.no_grad():
            weights = torch.load(self.embeddings_path)['weight']
            model.embedding_layer.weight.copy_(weights)

    def save_weights(self, node_id : int = 0):
        """
        Given a node id, saves the rnn and linear weights of the current node model. If the id is 0, will save 
        the general model.

        Parameters
        ----------
        - node_id : int
            The node to save to.
        """
        if node_id == 0:
            model = self.general_model
        else:
            model = self.user_model
        rnn_state_dict = model.rnn.state_dict()
        rnn_path = os.path.join(self.rnn_folder, 'rnn_general.pth' if node_id == 0 else f"rnn_{node_id}.pth")
        torch.save(rnn_state_dict, rnn_path)

        linear_state_dict = model.linear.state_dict()
        linear_path = os.path.join(self.linear_folder, 'linear_general.pth' if node_id == 0 else f"linear_{node_id}.pth")
        torch.save(linear_state_dict, linear_path)

        optim_stat_dict = model.optimizer.state_dict()
        optim_path = os.path.join(self.optim_folder, 'optim_general.pth' if node_id == 0 else f"optim_{node_id}.pth")
        torch.save(optim_stat_dict, optim_path)

    def load_weights(self, node_id : int = 0, model : NextWordPredictorModel = None):
        """
        Given a node id and a NextWordPredictoModel, loads the rnn and linear weights from the 
        corresponding node in the model. If the id is 0, will load to the general model. It also
        loads the optimizer specific to the id.

        Parameters
        ----------
        - node_id : int
            The id of the node
        - model : NextWordPredictorModel
            The model to load the weigths in
        """
        if model is None:
            model = self.general_model
        rnn_path = os.path.join(self.rnn_folder, 'rnn_general.pth' if node_id == 0 else f"rnn_{node_id}.pth")
        linear_path = os.path.join(self.linear_folder, 'linear_general.pth' if node_id == 0 else f"linear_{node_id}.pth")
        optim_path = os.path.join(self.optim_folder, 'optim_general.pth' if node_id == 0 else f"optim_{node_id}.pth")
        with torch.no_grad():
            rnn_state_dict = torch.load(rnn_path)
            model.rnn.load_state_dict(rnn_state_dict)

            linear_state_dict = torch.load(linear_path)
            model.linear.load_state_dict(linear_state_dict)

            optim_stat_dict = torch.load(optim_path)
            model.optimizer.load_state_dict(optim_stat_dict)

    def generate_general(self, start_text : str, num_words : int = 100):
        return self.general_model.generate(start_text=start_text, vocabulary = self.vocabulary, num_words=num_words)

    def generate_node(self, start_text : str, node_id : int, num_words : int = 100):
        self.load_weights(node_id, self.user_model)
        return self.user_model.generate(start_text=start_text, vocabulary = self.vocabulary, num_words=num_words)

    def train(self, num_rounds, save_results = True):
        self.results = {}
        for round in range(num_rounds+1):
            self.results[round] = {}
            print(f'round {round}')
            self.general_model.train()
            for param in self.general_model.parameters():
                param.grad = None
            self.nodes_epoch_step(round)
            self.general_model_update(round)
        if save_results:
            self.save_results()

    def select_nodes(self):
        ids = np.arange(1, self.federated_args['num_training_nodes'] + 1)
        np.random.shuffle(ids)
        index = int(self.federated_args['C'] * len(ids))
        rest = ids[index:]
        ids = ids[:index]
        
        # add empty metrics to unselected nodes:
        for node_id in rest:
            node = self.nodes[node_id]
            if len(node.losses['total_loss']) > 0:
                node.losses['total_loss'].append(node.losses['total_loss'][-1])
                node.losses['loss'].append(node.losses['loss'][-1])
                node.losses['reg_loss'].append(node.losses['reg_loss'][-1])
            else:
                node.losses['total_loss'].append(0)
                node.losses['loss'].append(0)
                node.losses['reg_loss'].append(0)
        return rest, ids

    def save_results(self):
        """
        Saves the results obtained during training
        """
        logging.info('saving results')
        results_path = self.federated_args['results_folder']
        path = os.path.join(results_path, self.pipeline_args['DATA_PARAMETERS']['data_name'])
        make_dir_if_not_exists(path)
        path = os.path.join(path, self.get_name())
        make_dir_if_not_exists(path)
        # path is of format *resultsFolder*/*dataType*/*FedAlg*/*date*_*id*/

        id_ = len(os.listdir(path))
        path = os.path.join(path, f"{str(date.today())}_{id_}")
        make_dir_if_not_exists(path)

        # Hyperparameters saving
        hyperparameters_path = os.path.join(path, "hyperparams.pickle")
        hyperparameters = self.pipeline_args
        hyperparameters['FEDERATED_ARGS'] = self.federated_args
        with open(hyperparameters_path, 'wb') as f:
            pickle.dump(hyperparameters, f)

        # history and results saving
        metrics_path = os.path.join(path, "metrics.pickle")
        metrics = self.results
        with open(metrics_path, 'wb') as f:
            pickle.dump(metrics, f)

        logging.info(f'results saved at {path}')

    @abstractclassmethod
    def prepare_models_for_training(self):
        raise NotImplementedError

    @abstractclassmethod
    def nodes_epoch_step(self, epoch):
        raise NotImplementedError

    @abstractclassmethod
    def general_model_update(self, round):
        raise NotImplementedError

    @abstractclassmethod
    def get_name(self):
        raise NotImplementedError










class Federated_AVG(Federated):
    def __init__(
        self,
        pipeline_args : str,
        federated_args : str,
        load_model_from = None,
        testing = False
    ):
        super(Federated_AVG, self).__init__(
            pipeline_args,
            federated_args,
            load_model_from,
            testing
        )
    def get_name(self):
        return 'FedAVG'

    def prepare_models_for_training(self):
        self.general_model.train()
        self.general_model.freeze_embeddings()  
        # Current general model is stored in state dict
        self.current_state_dict = self.general_model.state_dict()

    def nodes_epoch_step(self, epoch):
        total_data = 0
        self.agg_state_dict = None
        # We only select a subset of C * N nodes

        rest, ids = self.select_nodes()

        for node_id in tqdm(ids):
            # At the first epoch all nodes start from the init model
            node = self.nodes[node_id]
            total_data += len(node.data)
            # loads general model
            self.general_model.load_state_dict(self.current_state_dict)
            self.general_model.optimizer = torch.optim.Adam(
                lr=self.federated_args['node_model_lr'],
                params=self.general_model.parameters()
            )
            if epoch > 0:
                if isinstance(node, NormalModelForgingNode):
                    state_dict = torch.load(self.attack_model_path)
                    self.general_model.load_state_dict(state_dict)
                elif isinstance(node, StrategicModelForgingNode):
                    self.general_model.load_state_dict(node.compute_forged_model(
                        self.general_model,
                        self.model_parameters
                    ))
                else:
                    if isinstance(node, StrategicDataPoisoningNode):
                        node.compute_forged_model(self.general_model)
                        node.generate_poisoned_dataset(self.general_model)

                    node_dataloader = torch.utils.data.DataLoader(
                        node.data,
                        batch_size = self.pipeline_args['TRAINING_PARAMETERS']['batch_size'],
                        shuffle = True,
                        drop_last = True
                    )
                    for e in range(self.pipeline_args['TRAINING_PARAMETERS']['num_epochs']):
                        user_total_losses, user_losses, user_reg_losses = self.general_model.epoch_step(
                            node_dataloader,
                            node,
                            with_tqdm = False,
                            sep_losses=True
                        )
                    node.losses['total_loss'].append(np.mean(user_total_losses))
                    node.losses['loss'].append(np.mean(user_losses))
                    node.losses['reg_loss'].append(np.mean(user_reg_losses))

            node.state = self.general_model.state_dict()
        self.weigthed_avg(total_data, ids=ids)

    def weigthed_avg(self, total_data, ids = None):
        # perform node weighted average
        for i,node in self.nodes.items():
            if (ids is None) or (i in ids):
                node_state_dict = node.state
                ratio = len(node.data) / total_data
                if self.agg_state_dict is None:
                    self.agg_state_dict = node_state_dict
                    for key in self.agg_state_dict:
                        self.agg_state_dict[key] = self.agg_state_dict[key] * ratio
                else:
                    for key in self.agg_state_dict:
                        self.agg_state_dict[key] = self.agg_state_dict[key] + node_state_dict[key] * ratio

    def general_model_update(self, round):
        self.current_state_dict = self.agg_state_dict
        self.general_model.load_state_dict(self.current_state_dict)
        with torch.no_grad():
            self.evaluate_metrics(round)


    def evaluate_metrics(self, round):
        res = self.results[round]
        val_dataloader = torch.utils.data.DataLoader(
            self.val_dataset,
            batch_size = self.pipeline_args['TRAINING_PARAMETERS']['batch_size'],
            drop_last = True,
            shuffle = False
        )
        res[f'perplexity'],  res[f'loss'], res[f'f1_recall'], res[f'f3_recall'] \
            = self.general_model.perplexity(val_dataloader, with_tqdm = False, with_recall = True)
        res[f'generate'] = self.general_model.generate(self.vocabulary, 'all', 10)
        res[f'attack_perplexity'],_ = self.general_model.perplexity(self.attack_dataloader,with_tqdm = False,with_recall = False)

        for node_id, node in self.nodes.items():
            val_dataloader = self.get_node_dataloader(node, val = True)
            (
                res[f'perplexity_{node_id}'],  res[f'loss_{node_id}'], 
                res[f'f1_recall_{node_id}'], res[f'f3_recall_{node_id}']
            ) = self.general_model.perplexity(val_dataloader, with_tqdm = False, with_recall = True)
            res[f'generate_{node_id}'] = self.general_model.generate(self.vocabulary, 'all', 10)
            res[f'attack_perplexity_{node_id}'],_ = self.general_model.perplexity(
                self.attack_dataloader,
                with_tqdm = False,
                with_recall = False
            )










class Federated_LICCHAVI(Federated):
    def __init__(
        self,
        pipeline_args : str,
        federated_args : str,
        load_model_from = None,
        testing = False
    ):
        super(Federated_LICCHAVI, self).__init__(
            pipeline_args,
            federated_args,
            load_model_from,
            testing
        )
    def get_name(self):
        if self.nodes[1].p == 1:
            return 'LICCHAVI_L1'
        else:
            return 'LICCHAVI_L2'

    def models_difference(self, node : Node):
        """
        Computes the p normed difference between the general model and another
        for the parameters that require gradient excepting biases.
        """
        reg = torch.FloatTensor([0]).to(self.general_model.device)
        if node.lambda_ == 0:
            return reg
        else:
            reg.requires_grad = True
            for ((name, w1), (_, w2)) in zip(self.general_model.named_parameters(), self.user_model.named_parameters()):            
                if ('bias' not in name) and ('embedding' not in name):
                    reg = reg + node.lambda_ * torch.dist(w1, w2, node.p)
            return reg

    def init_user_model(self):
        self.model_parameters['LEARNING_RATE'] = self.federated_args['node_model_lr']
        self.user_model = init_model(None, **self.model_parameters)
        self.load_embeddings(self.user_model)
        self.user_model.train()
        self.user_model.general_regularizer = self.models_difference
        
    def prepare_models_for_training(self, share_embeddings = True):
        """
        Prepares the general model for training and setup the lamdba_0
        and p_0 paramters.
        """
        # Initialize the general model
        self.general_model.gamma = self.federated_args['lambda_0']
        self.general_model.q = self.federated_args['p_0']
        self.general_model.train()
        self.general_model.freeze_embeddings()

    def freeze_general_model(self):
        for p in self.general_model.parameters():
            p.requires_grad = False
    
    def unfreeze_general_model(self):
        for p in self.general_model.parameters():
            p.requires_grad = True
        self.general_model.freeze_embeddings()
    
    def freeze_node_model(self):
        for p in self.user_model.parameters():
            p.requires_grad = False
    
    def unfreeze_node_model(self):
        for p in self.user_model.parameters():
            p.requires_grad = True
        self.user_model.freeze_embeddings()

    def nodes_epoch_step(self, round):
        # we freeze the general model paramters to avoid update
        self.freeze_general_model()
        # We only select a subset of C * N nodes
        rest, ids = self.select_nodes()
        for node_id in tqdm(ids):
            node = self.nodes[node_id]
            node_dataloader = self.get_node_dataloader(node, val = False)
            # add general model reg
            self.init_user_model()
            if round == 0:
                pass
                #self.user_model.load_model(self.load_model_from)
            else:
                if isinstance(node, NormalModelForgingNode):
                    # loads the vicious model in the user model
                    state_dict = torch.load(self.attack_model_path)
                    self.user_model.load_state_dict(state_dict)
                elif isinstance(node, StrategicModelForgingNode):
                    # forges the model and then load is in the user model
                    state_dict = node.compute_forged_model(self.general_model)
                    self.user_model.load_state_dict(state_dict)
                else:
                    # forges the model and generates the data
                    if isinstance(node, StrategicDataPoisoningNode):
                        node.compute_forged_model(self.general_model)
                        node.generate_poisoned_dataset(self.general_model)
                    # Perform several data passes through data 
                    self.load_weights(node_id, self.user_model)
                    self.unfreeze_node_model()
                    for e in range(self.pipeline_args['TRAINING_PARAMETERS']['num_epochs']):
                        user_total_losses, user_losses, user_reg_losses = self.user_model.epoch_step(
                            node_dataloader,
                            node,
                            with_tqdm = False,
                            sep_losses = True
                        )
                    node.losses['total_loss'].append(np.mean(user_total_losses))
                    node.losses['loss'].append(np.mean(user_losses))
                    node.losses['reg_loss'].append(np.mean(user_reg_losses))

            if isinstance(node, UserNode):
                self.evaluate_metrics_node(node_id, node, round)
            self.save_weights(node_id) 
    
    def general_model_update(self, round):
        # unfreez general model parameters except embeddings
        for p in self.general_model.parameters():
            p.grad = None
        self.unfreeze_general_model()
        self.freeze_node_model()
        # UPDATE OF THE GENERAL MODEL GRADIENT
        # adds the general model regularization loss and its gradient
        if round >  0:
            general_model_reg_loss = self.general_model.regularizer()
            general_model_reg_loss.backward()
            for node_id, node in self.nodes.items():
                self.load_weights(node_id, self.user_model)
                for p in self.user_model.parameters():
                    p.requires_grad = False
                other_reg_loss = self.models_difference(node)
                if other_reg_loss > 0:
                    other_reg_loss.backward()
            self.general_model.optimizer.step()
        self.evaluate_metrics_general(round)


    def evaluate_metrics_node(self, node_id, node, round):
        val_dataloader = self.get_node_dataloader(node, val = True)
        res = self.results[round]
        (
            res[f'perplexity_{node_id}'],  res[f'loss_{node_id}'], 
            res[f'f1_recall_{node_id}'], res[f'f3_recall_{node_id}']
        ) = self.user_model.perplexity(val_dataloader, with_tqdm = False, with_recall = True)
        res[f'generate_{node_id}'] = self.user_model.generate(self.vocabulary, 'all', 10)
        res[f'attack_perplexity_{node_id}'],_ = self.user_model.perplexity(
            self.attack_dataloader,
            with_tqdm = False,
            with_recall = False
        )

    def evaluate_metrics_general(self, round):
        val_dataloader = torch.utils.data.DataLoader(
                self.val_dataset,
                batch_size = self.pipeline_args['TRAINING_PARAMETERS']['batch_size'],
                drop_last = True,
                shuffle = False
            )
        res = self.results[round]
        (
            res[f'perplexity'],  res[f'loss'], 
            res[f'f1_recall'], res[f'f3_recall']
        ) = self.general_model.perplexity(val_dataloader, with_tqdm = False, with_recall = True)
        res[f'generate'] = self.general_model.generate(self.vocabulary, 'all', 10)
        res[f'attack_perplexity'],_ = self.general_model.perplexity(
            self.attack_dataloader,
            with_tqdm = False,
            with_recall = False
        )














def grid_search(federated_alg, dataType):
    if dataType == 'tweet':
        model_file = "CONFIG_MODEL_TWEETS.json"
        fed_file = "CONFIG_FEDERATED_TWEETS.json"
    else:
        model_file = "CONFIG_MODEL_WIKI.json"
        fed_file = "CONFIG_FEDERATED_WIKI.json"
    i=0
    if federated_alg == 'FedAVG-':
        # For FedAVG, we need to grid search learning rates, nodes batch_size, nodes epochs and nodes proportion
        # These are the same hyperparameters tuned in the keyboard federated paper
        for lr in [1e-4]:
            for bs in [8]:
                for num_epochs in [1,2,3]:
                    for C in [1]:
                        for gamma in [1e-4, 1e-5, 1e-6]:
                            update_json(os.path.join('.','config_files', fed_file), 
                                node_model_lr = lr,
                                C = C,
                                results_folder = 'results'
                            )
                            update_json(os.path.join('.','config_files', model_file), 
                                TRAINING_PARAMETERS = {
                                    "fp16": 0,
                                    'batch_size' : bs,
                                    'num_epochs' : num_epochs
                                },
                                MODEL_PARAMETERS = {
                                    "fp16": 0,
                                    'gamma' : gamma
                                }
                            )
                            if i>=0:
                                federated = Federated_AVG(model_file, fed_file, testing=True)
                                logging.info(f'training {federated.get_name()} {dataType} for lr={lr}|bs={bs}|num_ep={num_epochs}|C={C}|\gamma={gamma}')
                                federated.train(5, save_results=True)
                            i+=1
    elif federated_alg =='LICCHAVI_L1':
        federated = Federated_LICCHAVI
        for model_lr in [1e-3]:
            for lambda_0 in [1e-6]:
                for node_lr in [1e-3]:
                    for num_epochs in [1]:
                        for C in [1]:
                            for bs in [8,16,32]:
                                for gamma in [1e-5]:
                                    update_json(
                                        os.path.join('.','config_files', fed_file),
                                        general_model_lr = model_lr,
                                        node_model_lr = node_lr,
                                        lambda_0 = lambda_0,
                                        lambda_n = 1,
                                        C = C,
                                        p_n = 1,
                                        results_folder = 'results'
                                    )
                                    update_json(os.path.join('.','config_files', model_file),
                                    MODEL_PARAMETERS = {
                                        "fp16": 0,
                                        'gamma' : lambda_0
                                    },
                                    TRAINING_PARAMETERS = {
                                        'batch_size' : bs,
                                        'num_epochs' : num_epochs,
                                        'fp16' : 0
                                    })
                                    if i>=0:
                                        federated = Federated_LICCHAVI(model_file, fed_file, testing=True)
                                        logging.info(f'training {federated.get_name()} {dataType} for bs={bs}|lam_0={lambda_0}')
                                        federated.train(5, save_results=True)
                                    i+=1

    elif federated_alg =='LICCHAVI_L2':
        federated = Federated_LICCHAVI
        for model_lr in [1e-3]:
            for lambda_0 in [1e-6]:
                for node_lr in [1e-3]:
                    for num_epochs in [1]:
                        for C in [1]:
                            for bs in [8,16,32]:
                                for gamma in [1e-5]:
                                    update_json(
                                        os.path.join('.','config_files', fed_file),
                                        general_model_lr = model_lr,
                                        node_model_lr = node_lr,
                                        lambda_0 = lambda_0,
                                        lambda_n = 1,
                                        C = C,
                                        p_n = 2,
                                        results_folder = 'results'
                                    )
                                    update_json(os.path.join('.','config_files', model_file),
                                    MODEL_PARAMETERS = {
                                        "fp16": 0,
                                        'gamma' : lambda_0
                                    },
                                    TRAINING_PARAMETERS = {
                                        'batch_size' : bs,
                                        'num_epochs' : num_epochs,
                                        "fp16" : 0
                                    })
                                    if i>=0:
                                        federated = Federated_LICCHAVI(model_file, fed_file, testing=True)
                                        logging.info(f'training {federated.get_name()} {dataType} for bs={bs}|lam_0={lambda_0}')
                                        federated.train(5, save_results=True)
                                    i+=1
        

def attack(federated_alg, dataType, attack_type):
    if dataType == 'tweet':
        model_file = "CONFIG_MODEL_TWEETS.json"
        fed_file = "CONFIG_FEDERATED_TWEETS.json"
        byzantine_datsize = 6000
    else:
        model_file = "CONFIG_MODEL_WIKI.json"
        fed_file = "CONFIG_FEDERATED_WIKI.json"

    NUM_ROUNDS = 20
    i=0
    if federated_alg == 'FedAVG-':
        # For FedAVG, we need to grid search learning rates, nodes batch_size, nodes epochs and nodes proportion
        # These are the same hyperparameters tuned in the keyboard federated paper
        lr = 1e-4
        bs = 32
        gamma = 1e-5
        num_epochs = 3
        C = 1
        for num_training_nodes in [50]:
            for f in [0, 0.1, 0.3, 0.5]:
                num_byzantine = int(num_training_nodes * f)
                update_json(os.path.join('.','config_files', fed_file),
                    general_model_lr = lr,
                    node_model_lr = lr,
                    C = C,
                    num_training_nodes = num_training_nodes,
                    num_byzantine = num_byzantine,
                    byzantine_datasize = byzantine_datsize,
                    byzantine_type = attack_type,
                    results_folder = 'attacks_results',
                )
                update_json(os.path.join('.','config_files', model_file), 
                TRAINING_PARAMETERS = {
                    "fp16": 0,
                    'batch_size' : bs,
                    'num_epochs' : num_epochs
                },
                MODEL_PARAMETERS = {
                    "fp16": 0,
                    'gamma' : gamma
                })
                if i>=0:
                    federated = Federated_AVG(model_file, fed_file, testing=True)
                    logging.info(f'training {federated.get_name()} {dataType} for lr={lr}|bs={bs}|num_ep={num_epochs}|C={C}|\gamma={gamma}')
                    federated.train(NUM_ROUNDS)
                i+=1
    elif federated_alg =='LICCHAVI_L1':
        federated = Federated_LICCHAVI
        node_model_lr = 1e-4
        general_model_lr = 1e-3
        lambda_0 = 1e-6
        lambda_n = 1
        num_epochs = 1
        C = 1
        bs = 32
        for num_training_nodes in [50]:
            for f in [0, 0.1, 0.3, 0.5]:
                num_byzantine = int(num_training_nodes * f)
                update_json(
                    os.path.join('.','config_files', fed_file),
                    general_model_lr = general_model_lr,
                    node_model_lr = node_model_lr,
                    lambda_0 = lambda_0,
                    lambda_n = lambda_n,
                    C = C,
                    p_n = 1, # this determines L1 loss
                    num_training_nodes = num_training_nodes,
                    num_byzantine = num_byzantine,
                    byzantine_type = attack_type,
                    results_folder = 'attacks_results'
                )
                update_json(os.path.join('.','config_files', model_file), 
                TRAINING_PARAMETERS = {
                    "fp16": 0,
                    'batch_size' : bs,
                    'num_epochs' : num_epochs
                },
                MODEL_PARAMETERS = {
                    "fp16": 0,
                    'gamma' : lambda_0
                })
                if i>=0:
                    federated = Federated_LICCHAVI(model_file, fed_file, testing=True)
                    logging.info(f'attack {federated.get_name()} {dataType} for f:{f} | K:{num_training_nodes}')
                    federated.train(NUM_ROUNDS)
                i+=1
    elif federated_alg =='LICCHAVI_L2':
        federated = Federated_LICCHAVI
        node_model_lr = 1e-4
        general_model_lr = 1e-3
        lambda_0 = 1e-6
        lambda_n = 1
        num_epochs = 1
        C = 1
        bs = 32
        for num_training_nodes in [50]:
            for f in [0, 0.1, 0.3, 0.5]:
                num_byzantine = int(num_training_nodes * f)
                update_json(
                    os.path.join('.','config_files', fed_file),
                    general_model_lr = general_model_lr,
                    node_model_lr = node_model_lr,
                    lambda_0 = lambda_0,
                    lambda_n = lambda_n,
                    C = C,
                    p_n = 2, # this determines L2 loss
                    num_training_nodes = num_training_nodes,
                    num_byzantine = num_byzantine,
                    byzantine_type = attack_type,
                    results_folder = 'attacks_results'
                )
                update_json(os.path.join('.','config_files', model_file), 
                TRAINING_PARAMETERS = {
                    "fp16": 0,
                    'batch_size' : bs,
                    'num_epochs' : num_epochs
                },
                MODEL_PARAMETERS = {
                    "fp16": 0,
                    'gamma' : lambda_0
                })
                if i>=0:
                    federated = Federated_LICCHAVI(model_file, fed_file, testing=True)
                    logging.info(f'attack {federated.get_name()} {dataType} for f:{f} | K:{num_training_nodes}')
                    federated.train(NUM_ROUNDS)
                i+=1

if __name__ == '__main__':
    logging.basicConfig(filename='logs/federated.log', level=logging.DEBUG)
    arguments = sys.argv

    if len(arguments) < 4:
        print('invalid arguments')
        sys.exit(1)
    elif arguments[1] in ['FedAVG', 'LICCHAVI_L1', 'LICCHAVI_L2']:
        if arguments[3] not in ['tweet', 'wiki']:
            print('invalid arguments')
            sys.exit(1)

        if arguments[2] == 'grid':
            grid_search(
                arguments[1],
                arguments[3]
            )
        elif arguments[2] == 'attack':
            attack(
                arguments[1],
                arguments[3],
                arguments[4]
            )
        else:
            print('invalid arguments')
            sys.exit(1)


    else:
        print('invalid arguments')
        sys.exit(1)