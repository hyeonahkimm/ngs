import os
import sys
import numpy as np
path_here = os.path.dirname(os.path.realpath(__file__))
sys.path.append(path_here)
sys.path.append('/'.join(path_here.rstrip('/').split('/')[:-2]))
from main.optimizer import BaseOptimizer
from utils import Variable, seq_to_smiles, unique
from model import RNN
from data_structs import Vocabulary, Experience, MolData, Variable
import torch
from rdkit import Chem
from tdc import Evaluator

from joblib import Parallel
from main.ngs.ga_operators import GeneticOperatorHandler


def sanitize(smiles):
    canonicalized = []
    for s in smiles:
        try:
            canonicalized.append(Chem.MolToSmiles(Chem.MolFromSmiles(s), canonical=True))
        except:
            pass
    return canonicalized


class NGS_Optimizer(BaseOptimizer):

    def __init__(self, args=None):
        super().__init__(args)
        self.model_name = "ngs"

    def _optimize(self, oracle, config):

        self.oracle.assign_evaluator(oracle)

        path_here = os.path.dirname(os.path.realpath(__file__))
        restore_prior_from=os.path.join(path_here, 'data/Prior.ckpt')
        restore_agent_from=restore_prior_from 
        voc = Vocabulary(init_from_file=os.path.join(path_here, "data/Voc"))

        Prior = RNN(voc)
        Agent = RNN(voc)

        # By default restore Agent to same model as Prior, but can restore from already trained Agent too.
        # Saved models are partially on the GPU, but if we dont have cuda enabled we can remap these
        # to the CPU.
        if torch.cuda.is_available():
            Prior.rnn.load_state_dict(torch.load(os.path.join(path_here,'data/Prior.ckpt')))
            Agent.rnn.load_state_dict(torch.load(restore_agent_from))
        else:
            Prior.rnn.load_state_dict(torch.load(os.path.join(path_here, 'data/Prior.ckpt'), map_location=lambda storage, loc: storage))
            Agent.rnn.load_state_dict(torch.load(restore_agent_from, map_location=lambda storage, loc: storage))

        # We dont need gradients with respect to Prior
        for param in Prior.rnn.parameters():
            param.requires_grad = False
            
        log_z = torch.nn.Parameter(torch.tensor([5.]).cuda())
        optimizer = torch.optim.Adam([{'params': Agent.rnn.parameters(), 
                                        'lr': config['learning_rate']},
                                    {'params': log_z, 
                                        'lr': config['lr_z']}])

        # For policy based RL, we normally train on-policy and correct for the fact that more likely actions
        # occur more often (which means the agent can get biased towards them). Using experience replay is
        # therefor not as theoretically sound as it is for value based RL, but it seems to work well.
        experience = Experience(voc, max_size=config['num_keep'])

        ga_handler = GeneticOperatorHandler(mutation_rate=config['mutation_rate'], 
                                            population_size=config['population_size'])
        pool = Parallel(n_jobs=config['num_jobs'])

        print("Model initialized, starting training...")

        step = 0
        patience = 0
        prev_n_oracles = 0
        stuck_cnt = 0
        
        while True:

            if len(self.oracle) > 100:
                self.sort_buffer()
                old_scores = [item[1][0] for item in list(self.mol_buffer.items())[:100]]
            else:
                old_scores = 0
            
            # Sample from Agent
            seqs, agent_likelihood, entropy = Agent.sample(config['batch_size'])

            # Remove duplicates, ie only consider unique seqs
            unique_idxs = unique(seqs)
            seqs = seqs[unique_idxs]
            agent_likelihood = agent_likelihood[unique_idxs]
            entropy = entropy[unique_idxs]

            # Get prior likelihood and score
            smiles = seq_to_smiles(seqs, voc)
            if config['valid_only']:
                smiles = sanitize(smiles)
            
            score = np.array(self.oracle(smiles))

            # early stopping
            if len(self.oracle) > 1000:
                self.sort_buffer()
                new_scores = [item[1][0] for item in list(self.mol_buffer.items())[:100]]
                if new_scores == old_scores:
                    patience += 1
                    if patience >= self.args.patience:
                        # self.log_intermediate(finish=True)
                        print('convergence criteria met, abort ...... ')
                        break
                else:
                    patience = 0

            # early stopping
            if prev_n_oracles < len(self.oracle):
                stuck_cnt = 0
            else:
                stuck_cnt += 1
                if stuck_cnt >= 10:
                    # self.log_intermediate(finish=True)
                    print('cannot find new molecules, abort ...... ')
                    break
            
            prev_n_oracles = len(self.oracle)

            # Then add new experience
            new_experience = zip(smiles, score)
            experience.add_experience(new_experience)

            # Experience Replay
            # First sample
            avg_loss = 0.
            if config['experience_replay'] and len(experience) > config['experience_replay']:
                for _ in range(config['experience_loop']):
                    if config['rank_coefficient'] > 0:
                        exp_seqs, exp_score = experience.rank_based_sample(config['experience_replay'], config['rank_coefficient'])
                    else:
                        exp_seqs, exp_score = experience.sample(config['experience_replay'])

                    exp_agent_likelihood, _ = Agent.likelihood(exp_seqs.long())
                    prior_agent_likelihood, _ = Prior.likelihood(exp_seqs.long())

                    reward = torch.tensor(exp_score).cuda()

                    exp_forward_flow = exp_agent_likelihood + log_z
                    exp_backward_flow = reward * config['beta']
                    loss = torch.pow(exp_forward_flow - exp_backward_flow, 2).mean()

                    # KL penalty
                    if config['penalty'] == 'prior_kl':
                        loss_p = (exp_agent_likelihood - prior_agent_likelihood).mean()
                        loss += config['kl_coefficient']*loss_p

                    # print(loss.item())
                    avg_loss += loss.item()/config['experience_loop']

                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()

            step += 1
            
            if len(self.oracle) >= config['starting_ga_from']:
                break
        
        
        ############## GA #################
        if config['starting_ga_from'] < 10000:
            print("Starting GA ...")
            stuck_cnt, patience = 0, 0
            
            while True:
                if len(self.oracle) < config["population_size"]:
                    # Exploration run
                    starting_population = np.random.choice(self.all_smiles, config["population_size"])

                    # select initial population
                    population_smiles = starting_population
                    population_mol = [Chem.MolFromSmiles(s) for s in population_smiles]
                    population_scores = self.oracle([Chem.MolToSmiles(mol) for mol in population_mol])
                    all_smis, all_scores = population_smiles, population_scores
                else:
        
                    self.oracle.sort_buffer()
                    all_smis, all_scores = tuple(map(list, zip(*[(smi, elem[0]) for (smi, elem) in self.oracle.mol_buffer.items()])))
                
                # log likelihood to measure novelty
                if config['use_novelty']:
                    all_smis, all_scores, all_seqs = smiles_to_seqs(all_smis, all_scores, voc)
                    
                    with torch.no_grad():
                        pop_likelihood, _ = Agent.likelihood(all_seqs.long())
                    all_novelty = (-1) * pop_likelihood.cpu().numpy()
                    # all_novelty = []
                    # for smi in all_smis:
                    #     # import pdb; pdb.set_trace()
                    #     ref = list(filter(lambda x: x not in set([smi]), all_smis))
                    #     novelty_score = novelty([smi], ref)
                    #     all_novelty.append(novelty_score[0])
                    # all_novelty = np.array(all_novelty)
                else:
                    all_novelty = None

                pop_smis, pop_scores, pop_novelty = ga_handler.select(all_smis, all_scores, all_novelty, rank_coefficient=config['rank_coefficient'], replace=False)
                
                for _ in range(config['reinitiation_interval']):
                    child_smis = ga_handler.query(
                            query_size=config['offspring_size'], mating_pool=(pop_smis, pop_scores), pool=pool, model=Agent,
                            rank_coefficient=config['rank_coefficient'],
                        )

                    child_score = np.array(self.oracle(child_smis))
                    # print(len(self.oracle), '| child_score:', child_score.mean(), child_score.max())
                    
                    # log likelihood to measure novelty
                    valid_child_smis, valid_child_score, valid_child_seqs = smiles_to_seqs(child_smis, child_score, voc)
                    
                    if config['use_novelty']:
                        with torch.no_grad():
                            child_likelihood, _ = Agent.likelihood(valid_child_seqs.long())
                        child_novelty = (-1) * child_likelihood.cpu().numpy()
                        # child_novelty = novelty(valid_child_smis, pop_smis)
                    else:
                        child_novelty = None
                
                    new_experience = zip(child_smis, child_score)
                    experience.add_experience(new_experience)
                    pop_smis, pop_scores, _ = ga_handler.select(pop_smis+valid_child_smis, 
                                                                            pop_scores+valid_child_score, 
                                                                            np.concatenate([pop_novelty, child_novelty]) if config['use_novelty'] else None, 
                                                                            rank_coefficient=config['rank_coefficient'], 
                                                                            replace=False)
                    
                    pop_novelty = []
                    for smi in pop_smis:
                        ref = list(filter(lambda x: x != smi, pop_smis))
                        novelty_score = novelty([smi], ref)
                        pop_novelty.append(novelty_score[0])
                    pop_novelty = np.array(pop_novelty)
                    # population = (tuple(map(list, zip(*[(smi, elem[0]) for (smi, elem) in self.oracle.mol_buffer.items()]))))

                    if self.finish:
                        print('max oracle hit')
                        break
                    
                    if config['update_during_ga']:
                        # Experience Replay
                        # First sample
                        avg_loss = 0.
                        if config['experience_replay'] and len(experience) > config['experience_replay']:
                            for _ in range(config['experience_loop']):
                                if config['rank_coefficient'] > 0:
                                    exp_seqs, exp_score = experience.rank_based_sample(config['experience_replay'], config['rank_coefficient'])
                                else:
                                    exp_seqs, exp_score = experience.sample(config['experience_replay'])

                                exp_agent_likelihood, _ = Agent.likelihood(exp_seqs.long())
                                prior_agent_likelihood, _ = Prior.likelihood(exp_seqs.long())

                                reward = torch.tensor(exp_score).cuda()

                                exp_forward_flow = exp_agent_likelihood + log_z
                                exp_backward_flow = reward * config['beta']
                                
                                # exp_backward_flow += prior_agent_likelihood.detach()  # rtb-style
                                loss = torch.pow(exp_forward_flow - exp_backward_flow, 2).mean()

                                # KL penalty
                                if config['penalty'] == 'prior_kl':
                                    loss_p = (exp_agent_likelihood - prior_agent_likelihood).mean()
                                    loss += config['kl_coefficient']*loss_p

                                # print(loss.item())
                                avg_loss += loss.item()/config['experience_loop']

                                optimizer.zero_grad()
                                loss.backward()
                                # grad_norms = torch.nn.utils.clip_grad_norm_(Agent.rnn.parameters(), 1.0)
                                optimizer.step()
                        
                # early stopping
                if len(self.oracle) > 1000:
                    self.sort_buffer()
                    new_scores = [item[1][0] for item in list(self.mol_buffer.items())[:100]]
                    if new_scores == old_scores:
                        patience += 1
                        # ga_handler.temp = min(0.2 + ga_handler.temp, 2.0)
                        if patience >= self.args.patience:
                            self.log_intermediate(finish=True)
                            print('convergence criteria met, abort ...... ')
                            break
                    else:
                        patience = 0
                        ga_handler.temp = 1

                # early stopping
                if prev_n_oracles < len(self.oracle):
                    stuck_cnt = 0
                else:
                    stuck_cnt += 1
                    if stuck_cnt >= 10:
                        self.log_intermediate(finish=True)
                        print('cannot find new molecules, abort ...... ')
                        break
                    
                if self.finish:
                    print('max oracle hit')
                    break
                
                prev_n_oracles = len(self.oracle)
                step += 1
        

def smiles_to_seqs(smiles, scores, voc, unique=False):
    valid_seqs, valid_scores, valid_smis = [], [], []
    for i, smi in enumerate(smiles):
        if unique and smi in valid_smis:
            continue
        try:
            tokenized = voc.tokenize(smi)
            valid_seqs.append(Variable(voc.encode(tokenized)))
            valid_scores.append(scores[i])
            valid_smis.append(smi)
        except:
            pass
    valid_seqs = MolData.collate_fn(valid_seqs)
    
    return valid_smis, valid_scores, valid_seqs


def novelty(new_smiles, ref_smiles):
    evaluator = Evaluator(name = 'Diversity')  # pairwise
    novelty_scores = []
    for d in new_smiles:
        dist = np.array([evaluator([d, od]) for od in ref_smiles])
        score = np.nan_to_num(dist).min()
        novelty_scores.append(score)
    # novelty_scores = np.nan_to_num(np.array(novelty_scores))
    return novelty_scores