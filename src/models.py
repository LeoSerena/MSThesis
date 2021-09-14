import os

from tqdm import tqdm
import numpy as np

from apex import amp, optimizers
import torch

class NextWordPredictorModel(torch.nn.Module):
    def __init__(
        self,
        emb_dim : int,
        vocab_size : int,
        num_lstm_hidden_layers : int,
        hidden_state_size : int,
        dropout : float,
        device : str,
        fp16 : bool = False,
        lr : float = 1e-3,
        weight : list = None
    ):
        super().__init__()
        self.lr = lr
        self.num_lstm_hidden_layers = num_lstm_hidden_layers
        self.hidden_state_size = hidden_state_size
        self.device = device
        self.fp16 = fp16
        self.vocab_size = vocab_size
        # Embedding layer
        self.embedding_layer = torch.nn.Embedding(
            self.vocab_size,
            emb_dim,
            padding_idx = 0
        ).to(device)
        # LSTM layer (later replace with oupled Input and Forget Gate (CIFG) maybe)
        self.lstm = torch.nn.LSTM(
            input_size = emb_dim,
            hidden_size = hidden_state_size,
            num_layers = num_lstm_hidden_layers,
            dropout = dropout,
            batch_first = True # -> input of the shape (bath size, seq length, emb length)
        ).to(device)
        # FFN for classification on vocab
        self.linear1 = torch.nn.Linear(
            hidden_state_size,
            hidden_state_size
        ).to(device)
        self.tanh = torch.nn.Tanh().to(device)
        self.dropout = torch.nn.Dropout(p = dropout).to(device)
        self.linear2 = torch.nn.Linear(
            hidden_state_size,
            self.vocab_size
        ).to(device)
        
        self.optimizer = torch.optim.SGD(self.parameters(), lr=self.lr)
        
        self.criterion = torch.nn.CrossEntropyLoss(
            weight = torch.FloatTensor([weight]).to(self.device) if weight is not None else None,
            ignore_index = 0,
            reduction = 'mean'
        ).to(device) # may use the weight as prior n_occ / num_words
        
        self.scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, 1.0, gamma=0.99)
        
    def forward(self, inputs, hidden = None):
        if hidden is None:
            hidden = self.init_hidden(inputs.shape[0])
        embeddings = self.embedding_layer(inputs)
        output, hidden = self.lstm(embeddings, (hidden[0].detach(), hidden[1].detach()))
        output = self.dropout(self.tanh(self.linear1(output)))
        output = self.linear2(output)
        return output, hidden
    
    def init_weights(self) -> None:
        initrange = 0.1
        self.embedding_layer.weight.data.uniform_(-initrange, initrange)
        self.linear1.bias.data.zero_()
        self.linear1.weight.data.uniform_(-initrange, initrange)
        self.linear2.bias.data.zero_()
        self.linear2.weight.data.uniform_(-initrange, initrange)
    
    def save_model(self, path = None):
        """
        Saves the model in the given path. If no path is given, automatically saved
        in the log_dir specified at training.
        """
        if path:
            torch.save(self.state_dict(), path)
        else:
            if hasattr(self, 'log_dir'):
                torch.save(self.state_dict(), os.path.join(self.log_dir, 'model.pth'))
            else:
                print('No path given, please enter a path to save the model.')
    
    def load_model(self, path = None):
        """
        Loads the model from the given path. If no path is given, automatically loaded
        from the log_dir specified at training.
        """
        if path:
            self.load_state_dict(torch.load(path), strict = False)
        else:
            if hasattr(self, 'log_dir'):
                self.load_state_dict(torch.load(os.path.join(self.log_dir, 'model.pth')))
            else:
                print('No path given, please enter a path to load the model.')
    
    def init_hidden(self, batch_size):
        return (
            torch.zeros(
                self.num_lstm_hidden_layers, batch_size, self.hidden_state_size
            ).to(self.device),
            torch.zeros(
                self.num_lstm_hidden_layers, batch_size, self.hidden_state_size
            ).to(self.device)
        )
    
    def evaluate(self, eval_dataloader, hidden):
        self.eval()
        losses = []
        self.init_hidden(eval_dataloader.batch_size)
        with torch.no_grad():
            for batch in tqdm(eval_dataloader):
                
                outputs, hidden = self.forward(batch[:,:-1], hidden)
                outputs = torch.transpose(outputs, 1,2)
                labels = batch[:,1:]
                
                loss = self.criterion(outputs, labels)
                
                losses.append(loss.item())
                
        return np.mean(losses)
        
    def epoch_step(self, data_loader, hidden):
        self.train()
        losses = []
        
        for batch_idx, batch in enumerate(tqdm(data_loader)):
            for param in self.parameters():
                param.grad = None
            outputs, hidden = self.forward(batch[:,:-1], hidden)
            outputs = torch.transpose(outputs, 1, 2)
            labels = batch[:,1:]
            
            loss = self.criterion(outputs, labels)
            
            if self.fp16:
                with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                    scaled_loss.backward()
                self.optimizer.step()
            else:
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.parameters(), 0.5)
                self.optimizer.step()
                
            losses.append(loss.item())
        
        self.scheduler.step()
        
        return losses
    
    def update_early_stopping(self, current_metric, epoch):
        if self.early_stopping_metric_best == 'min':
            is_better = self.best_metric > current_metric
        else:
            is_better = self.best_metric < current_metric
        if is_better:
            print('updating best metric')
            self.best_metric = current_metric
            self.best_epoch = epoch
            self.early_stopping_count = 0
#             self.save_model()
        else:
            self.early_stopping_count+=1
        if self.early_stopping_count == self.early_stopping_patience:
            print('early stopping, patience: {}, loading best epoch: {}.'.format(
                self.early_stopping_patience,
                self.best_epoch
            ))
#             if self.load_best:
#                 self.load_model()
            return 1
        else:
            return 0
    
    def fit(
        self, 
        train_dataloader,
        eval_dataloader,
        num_epochs = 30,
        early_stopping = True,
        early_stopping_patience = 3,
        early_stopping_metric = 'val_loss',
        early_stopping_metric_best = 'min', # if lower is better (like for loss)
    ):
        self.init_weights()
        hidden = self.init_hidden(train_dataloader.batch_size)
        if early_stopping:
            self.early_stopping_patience = early_stopping_patience
            self.early_stopping_metric_best = early_stopping_metric_best
            self.early_stopping_count = 0
            self.best_epoch = 0
            self.best_metric = np.inf if early_stopping_metric_best == 'min' else -np.inf
            # self.load_best = load_best
            
        metrics = {}
        
        for epoch in range(0, num_epochs+1):
            if epoch > 0:
                losses = self.epoch_step(train_dataloader, hidden)
                train_loss = np.mean(losses)
            else:
                train_loss = self.evaluate(train_dataloader, hidden)
            metrics[epoch] = {'train_loss' : train_loss}
            eval_loss = self.evaluate(eval_dataloader, hidden)
            metrics[epoch]['val_loss'] = eval_loss
            metrics[epoch]['lr'] = self.scheduler.get_last_lr()[0]
            print(f"Train loss at epoch {epoch} : {train_loss}")
            print(f"Eval loss at epoch {epoch} : {eval_loss}")
            if early_stopping:
                current_metric = metrics[epoch][early_stopping_metric]
                if self.update_early_stopping(current_metric, epoch):
                    break
                    
        return metrics