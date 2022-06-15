import torch
import os
import copy
import pandas as pd
import torch.nn as nn
from torch.autograd import Variable
import torch.optim as optim
from torch.utils.data import TensorDataset
from torch.utils.data import DataLoader
import torch.nn.functional as F
from transformers import AdamW, get_linear_schedule_with_warmup
from transformers import BertTokenizer,BertConfig,BertForTokenClassification,BertModel,AlbertModel,AlbertTokenizer
import time,datetime
from sklearn.metrics import precision_score,classification_report,f1_score,recall_score
import numpy as np
from torch.nn import CrossEntropyLoss, MSELoss
# from transformers import AlbertConfig, AlbertModel,AlbertForTokenClassification
from sklearn.model_selection import train_test_split
from tqdm import tqdm, trange
from sklearn.preprocessing import MultiLabelBinarizer
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from sklearn import metrics

base = './ResumeNER'
base_path = '/root/berts/chinese-roberta-wwm-ext'
train_path = 'train.char.bmes'
dev_path = 'dev.char.bmes'
test_path = 'test.char.bmes'

def load_data(base,train_path):
    full = os.path.join(base,train_path)
    with open(full,'r',encoding='utf-8')as f:
        data = f.readlines()
    tokens,labels = [],[]
    token,label = [],[]
    for line in data:
        line= line.strip().replace("\n",'')
        if len(line.split(' ')) == 2:
            token.append(line.split(' ')[0])
            label.append(line.split(' ')[1])
        else:
            tokens.append(token)
            labels.append(label)
            token,label = [],[]
    return tokens,labels

def trans2id(labels):
    tag_set = set()
    for line in labels:
        for label  in line:
            if label not in tag_set:
                tag_set.add(label)
    # tag_set.add('[CLS]')
    # tag_set.add('[SEP]')
    tag_set = list(tag_set)
    idx = [i for i in range(len(tag_set))]
    tag2id = dict(zip(tag_set,idx))
    id2tag = dict(zip(idx,tag_set))
    return tag2id,id2tag

def gen_features(tokens,labels,tokenizer,tag2id,max_len):
    tags,input_ids,token_type_ids,attention_masks,lengths = [],[],[],[],[]
    for i,(token,label) in enumerate(zip(tokens,labels)):
        sentence = ''.join(token)
        lengths.append(len(sentence))
        if len(token) >= max_len - 2:
            label = labels[i][0:max_len - 2]
        label = [tag2id['O']] + [tag2id[i] for i in label] + [tag2id['O']]
        if len(label) < max_len:
            label = label + [tag2id['O']] * (max_len - len(label))

        assert len(label) == max_len
        tags.append(label)

        inputs = tokenizer.encode_plus(sentence, max_length=max_len,pad_to_max_length=True,return_tensors='pt')
        input_id,token_type_id,attention_mask = inputs['input_ids'],inputs['token_type_ids'],inputs['attention_mask']
        input_ids.append(input_id)
        token_type_ids.append(token_type_id)
        attention_masks.append(attention_mask)
    return input_ids,token_type_ids,attention_masks,tags,lengths

max_len = 128
bs = 32
tokenizer = BertTokenizer.from_pretrained(base_path)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

train_tokens,train_labels = load_data(base,train_path)
tag2id,id2tag = trans2id(train_labels)
train_ids,train_token_type_ids,train_attention_masks,train_tags,train_lengths = gen_features(train_tokens,train_labels,tokenizer,tag2id,max_len)

dev_tokens,dev_labels = load_data(base,dev_path)
dev_ids,dev_token_type_ids,dev_attention_masks,dev_tags,dev_lengths = gen_features(dev_tokens,dev_labels,tokenizer,tag2id,max_len)

class FewShot_NER(nn.Module):
    def __init__(self,base_model_path,tag2id,batch_size):
        super().__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.token_encoder = BertModel.from_pretrained(base_model_path).to(self.device)
        self.label_encoder = BertModel.from_pretrained(base_model_path).to(self.device)
        self.label_context = {
            "EDU":"学历",
            "NAME":"姓名",
            "TITLE":"职称",
            "CONT":"国籍",
            "ORG":"组织",
            "LOC":"地区",
            "PRO":"产品",
            "RACE":"种族"
        }
        self.index_context = {
            "B":"开始词",
            "M":"中间词",
            "E":"结束词",
            "S":"单字词"
        }
        self.tokenizer = BertTokenizer.from_pretrained(base_path)
        # self.label_representation = self.build_label_representation(tag2id)
        self.label_representation = self.build_label_representation(tag2id).to(self.device)
        self.batch_size = batch_size

    def build_label_representation(self,tag2id):
        labels = []
        for k,v in tag2id.items():
            if k.split('-')[-1] != 'O':
                idx,label = k.split('-')[0],k.split('-')[-1]
                label = self.label_context[label]
                labels.append(label+self.index_context[idx])
            else:
                labels.append("其他类别词")
        '''
        mutul(a,b) a和b维度是否一致的问题
        A.shape =（b,m,n)；B.shape = (b,n,k)
        torch.matmul(A,B) 结果shape为(b,m,k)
        '''
        tag_embeddings = []
        for label in labels:
            input_ids = tokenizer.encode_plus(label,return_tensors='pt')
            outputs = self.label_encoder(input_ids=input_ids['input_ids'].to(self.device),
                                         token_type_ids=input_ids['token_type_ids'].to(self.device),attention_mask = input_ids['attention_mask'].to(self.device))
            last_hidden_states = outputs.last_hidden_state.detach()
            # print(label_representation.shape,last_hidden_states.shape)
            tag_embeddings.append(last_hidden_states)
            # label_representation = torch.cat((label_representation,last_hidden_states),0)
        label_embeddings = torch.stack(tag_embeddings,dim=0)
        label_embeddings = label_embeddings.squeeze(1)
        # print('label_embeddings shape',label_embeddings.shape)
        label_representation = label_embeddings
        return label_representation[:,0,:]

    def forward(self,inputs):
        # print(inputs['input_ids'].shape)
        outputs = self.token_encoder(input_ids=inputs['input_ids'],
                                     token_type_ids=inputs['token_type_ids'],attention_mask = inputs['attention_mask'])
        token_embeddings = outputs.last_hidden_state
        # print(token_embeddings.shape,self.label_representation.shape)
        # print(token_embeddings.device,self.label_representation.device)
        tag_lens,hidden_size = self.label_representation.shape
        current_batch_size  = token_embeddings.shape[0]
        label_embedding = self.label_representation.expand(current_batch_size,tag_lens,hidden_size)
        # print(label_embedding.device)
        label_embeddings = label_embedding.transpose(2,1)
        # print(label_embedding.device)
        # print(token_embeddings.shape,label_embedding.shape)
        # matrix_embeddings shape torch.Size([bs, max_len, tag_len])
        matrix_embeddings = torch.matmul(token_embeddings,label_embeddings)
        # print('matrix_embeddings shape',matrix_embeddings.shape,matrix_embeddings.device)
        softmax_embedding= nn.Softmax(dim=-1)(matrix_embeddings)
        # print(softmax_embedding.device)
        label_indexs = torch.argmax(softmax_embedding,dim=-1)
        # print(label_indexs.device)
        #（bs,label_lengths）
        # print('label_indexs',label_indexs)
        return matrix_embeddings,label_indexs

train_ids = torch.tensor([item.cpu().detach().numpy() for item in train_ids]).squeeze()
train_tags = torch.tensor(train_tags)
train_masks = torch.tensor([item.cpu().detach().numpy() for item in train_attention_masks]).squeeze()
train_token_type_ids = torch.tensor([item.cpu().detach().numpy() for item in train_token_type_ids]).squeeze()
# print(train_ids.shape,train_tags.shape,train_masks.shape,train_token_type_ids.shape)

dev_ids = torch.tensor([item.cpu().detach().numpy() for item in dev_ids]).squeeze()
dev_tags = torch.tensor(dev_tags)
dev_masks = torch.tensor([item.cpu().detach().numpy() for item in dev_attention_masks]).squeeze()
dev_token_type_ids = torch.tensor([item.cpu().detach().numpy() for item in dev_token_type_ids]).squeeze()

train_data = TensorDataset(train_ids, train_masks, train_token_type_ids,train_tags)
train_sampler = RandomSampler(train_data)
train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=bs)

valid_data = TensorDataset(dev_ids, dev_masks,dev_token_type_ids,dev_tags)
valid_sampler = RandomSampler(valid_data)
valid_dataloader = DataLoader(valid_data, sampler=valid_sampler, batch_size=bs)

fewshot = FewShot_NER(base_path,tag2id,bs)

optimizer = torch.optim.Adam(fewshot.parameters(),
                  lr = 5e-5 # default is 5e-5
                  # eps = 1e-8 # default is 1e-8
                )

epochs = 200
total_steps = len(train_dataloader) * epochs
scheduler = get_linear_schedule_with_warmup(optimizer,
                                           num_warmup_steps = 0,
                                           num_training_steps = total_steps)
fewshot.to(device)

def trans2label(id2tag,data,lengths):
    new = []
    for i,line in enumerate(data):
        tmp = [id2tag[word] for word in line]
        tmp = tmp[1:1 + lengths[i]]
        new.append(tmp)
    return new

def get_entities(tags):
    start, end = -1, -1
    prev = 'O'
    entities = []
    n = len(tags)
    tags = [tag.split('-')[1] if '-' in tag else tag for tag in tags]
    for i, tag in enumerate(tags):
        if tag != 'O':
            if prev == 'O':
                start = i
                prev = tag
            elif tag == prev:
                end = i
                if i == n -1 :
                    entities.append((start, i))
            else:
                entities.append((start, i - 1))
                prev = tag
                start = i
                end = i
        else:
            if start >= 0 and end >= 0:
                entities.append((start, end))
                start = -1
                end = -1
                prev = 'O'
    return entities

def measure(preds,trues,lengths,id2tag):
    correct_num = 0
    predict_num = 0
    truth_num = 0
    pred = trans2label(id2tag,preds,lengths)
    true = trans2label(id2tag,trues,lengths)
    # print(len(pred),len(true))
    assert len(pred) == len(true)
    for p,t in zip(pred,true):
        pred_en = get_entities(p)
        true_en = get_entities(t)
        correct_num += len(set(pred_en) & set(true_en))
        predict_num += len(set(pred_en))
        truth_num += len(set(true_en))
    precision = correct_num / predict_num if predict_num else 0
    recall = correct_num / truth_num if truth_num else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0
    return f1, precision, recall

max_grad_norm = 1.0
F1_score = 0

loss_function=CrossEntropyLoss()
def define_loss_function(input,target):

    logsoftmax_func=nn.LogSoftmax(dim=1)
    logsoftmax_output=logsoftmax_func(input)

    nllloss_func=nn.NLLLoss()
    nlloss_output=nllloss_func(logsoftmax_output,target)
    return nlloss_output

tra_loss,steps = 0.0,0
for i in range(epochs):
    fewshot.train()
    for step ,batch in enumerate(train_dataloader):
        input_ids,masks,token_type_ids,labels= (i.to(device) for i in batch)
        matrix_embeddings,label_indexs = fewshot({"input_ids":input_ids,"attention_mask":masks,"token_type_ids":token_type_ids})
        # print(outputs.float().dtype,labels.float().dtype)
        loss = loss_function(matrix_embeddings.view(-1, 28),labels.view(-1)) # CrossEntropyLoss

        loss.backward()

        tra_loss += loss
        steps += 1

        torch.nn.utils.clip_grad_norm_(parameters=fewshot.parameters(), max_norm=max_grad_norm)
        optimizer.step()
        scheduler.step()

        if step % 30 == 0:
            print("epoch :{},step :{} ,Train loss: {}".format(i,step,tra_loss/steps))

    print("Training Loss of epoch {}:{}".format(i,tra_loss / steps))

    fewshot.eval()
    dev_loss = 0.0
    predictions , true_labels = [], []

    for batch in valid_dataloader:
        input_ids,masks,token_type_ids,labels= (i.to(device) for i in batch)

        with torch.no_grad():
            matrix_embeddings,output_indexs = fewshot({"input_ids":input_ids,"attention_mask":masks,"token_type_ids":token_type_ids})

        # scores = scores.detach().cpu().numpy()
        predictions.extend(output_indexs.detach().cpu().numpy().tolist())
        true_labels.extend(labels.to('cpu').numpy().tolist())
#         lengths = lengths.detach().cpu().numpy().tolist()
#     dev_lengths = dev_lengths.detach().cpu().numpy()
    f1, precision, recall = measure(predictions,true_labels,dev_lengths,id2tag)
    print('epoch {} : Acc : {},Recall : {},F1 :{}'.format(i,precision,recall,f1))

    if F1_score < f1:
        F1_score = f1
        torch.save(fewshot.state_dict(), 'save_models/model_{}_{}.pth'.format(i,F1_score))

test_tokens,test_labels = load_data(base,test_path)
test_ids,test_token_type_ids,test_attention_masks,test_tags,test_lengths = gen_features(test_tokens,test_labels,tokenizer,tag2id,max_len)

test_ids = torch.tensor([item.cpu().detach().numpy() for item in test_ids]).squeeze()
test_tags = torch.tensor(test_tags)
test_masks = torch.tensor([item.cpu().detach().numpy() for item in test_attention_masks]).squeeze()
test_token_type_ids = torch.tensor([item.cpu().detach().numpy() for item in test_token_type_ids]).squeeze()

test_data = TensorDataset(test_ids, test_masks,test_token_type_ids, test_tags)
# test_sampler = RandomSampler(test_data)
test_dataloader = DataLoader(test_data, batch_size=bs)

fewshot.eval()
test_pre,test_true = [],[]
for batch in test_dataloader:

    input_ids,masks,token_type_ids,labels= (i.to(device) for i in batch)

    with torch.no_grad():
        matrix_embeddings,output_indexs = fewshot({"input_ids":input_ids,"attention_mask":masks,"token_type_ids":token_type_ids})

    test_pre.extend(output_indexs.detach().cpu().numpy().tolist())
    test_true.extend(labels.to('cpu').numpy().tolist())
test_f1, test_precision, test_recall = measure(test_pre,test_true,test_lengths,id2tag)
print('Test Acc : {},Recall : {},F1 :{}'.format(test_precision,test_recall,test_f1))

