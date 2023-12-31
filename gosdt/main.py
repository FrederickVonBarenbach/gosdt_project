import pandas as pd
import numpy as np
import time as t
import pathlib
import argparse
from sklearn.ensemble import GradientBoostingClassifier
import numpy.random as random
from gosdt.model.threshold_guess import compute_thresholds, cut, fit_boosted_tree
from gosdt.model.gosdt import GOSDT

SAMPLE_TYPES = ['sampling', 'deterministic', 'mathias', 'baseline', 
                'resample_weight_deterministic', 'resample_weight_baseline',
                'no_weights_vs_weights']
WEIGHTING_TYPES = ['exponential']

threshold_before = False 

# TODO:
# - change weights
# - make sure samplers are correct


def weighted_loss(model, X_train_dup, y_train_dup, X_train, y_train, weights):
    regularizer = model.tree.loss() - model.error(X_train_dup, y_train_dup)
    # TODO: some error here
    return model.error(X_train, y_train, weight=weights)


def apply_thresholds(dataset, thresholds, n_est, d, lr=0.1, backselect=True):
    X, y = dataset.iloc[:,:-1].values, dataset.iloc[:,-1].values
    X = pd.DataFrame(X, columns=dataset.columns[:-1])
    # apply thresholds
    X_new = cut(X, thresholds)
    clf1, out1 = fit_boosted_tree(X_new, y, n_est, lr, d)
    # do backselect (TODO: what is this? I think regularization??)
    outp = 1
    Xp = X_new.copy()
    clfp = clf1
    itr=0
    if backselect:
        while outp >= out1 and itr < X_new.shape[1]-1:
            vi = clfp.feature_importances_
            if vi.size > 0:
                c = Xp.columns
                i = np.argmin(vi)
                Xp = Xp.drop(c[i], axis=1)
                clfp, outp = fit_boosted_tree(Xp, y, n_est, lr, d)
                itr += 1
            else:
                break
        Xp[c[i]] = X_new[c[i]]
        #_, _ = fit_boosted_tree(Xp, y, n_est, lr, d)
    #print('features:', h)
    return Xp, pd.DataFrame(y)


# returns model
def preprocess_dataset(dataset, n_est, max_depth):
    X, y = dataset.iloc[:,:-1].values, dataset.iloc[:,-1].values
    # guess thresholds
    X = pd.DataFrame(X, columns=dataset.columns[:-1])
    # print("X:", X.shape)
    # print("y:",y.shape)
    X_train, thresholds, header, threshold_guess_time = compute_thresholds(X, y, n_est, max_depth)
    y_train = pd.DataFrame(y)
    # print("Thresholds:")
    # print(thresholds)
    # print(header)

    # guess lower bound
    clf = GradientBoostingClassifier(n_estimators=n_est, max_depth=max_depth, random_state=42)
    clf.fit(X_train, y_train.values.flatten())
    warm_labels = clf.predict(X_train)

    # save the labels as a tmp file and return the path to it.
    labelsdir = pathlib.Path('/tmp/warm_lb_labels')
    labelsdir.mkdir(exist_ok=True, parents=True)

    labelpath = labelsdir / 'warm_label.tmp'
    labelpath = str(labelpath)
    pd.DataFrame(warm_labels, columns=["class_labels"]).to_csv(labelpath, header="class_labels",index=None)

    # train GOSDT model
    config = {
                "regularization": 0.001,
                "depth_budget": 5,
                "time_limit": 60,
                "warm_LB": True,
                "path_to_labels": labelpath,
                "similar_support": False,
            }

    return GOSDT(config), X_train, y_train, thresholds


def perform_tree_fitting(data_dup, data, weights, n_est=40, max_depth=1, return_model=False, return_thresholds=False):
    # get thresholded data
    model, X_dup, y_dup, thresholds = preprocess_dataset(data_dup, n_est, max_depth)
    X, y = apply_thresholds(data, thresholds, n_est, max_depth)

    # fit on dupped data
    model.fit(X_dup, y_dup)

    print("evaluate the model, extracting tree and scores") 

    # get the results
    train_loss = weighted_loss(model, X_dup, y_dup, X, y, weights)

    print(f"Training loss: {train_loss}")
    # append the necessary return stuff
    ret_val = train_loss
    if return_model:
        if type(ret_val) is tuple:
            ret_val += (model,)
        else:
            ret_val = (ret_val, model)
    if return_thresholds:
        if type(ret_val) is tuple:
            ret_val += (thresholds,)
        else:
            ret_val = (ret_val, thresholds)
    return ret_val


def calc_weighted_loss(correct, weights):
    loss = 0
    for i, v in enumerate(correct):
        if not v:
            loss += weights[i]
    
    return loss


def sample_two_gamma_dists(preds, beta_right, beta_wrong):
    ret = []
    for v, i in enumerate(preds):
        if v:
            ret.append(np.random.gamma(beta_right, 0.25))
        else:
            ret.append(np.random.gamma(beta_wrong, 0.25))
    ret = np.array(ret)
    return ret

def resample_and_compare_deterministic(data, weights, p):
    data_cp = data.copy(deep=True)

    N = data.shape[0]
    # print(f"N: {N}\t N':{N*p}")
    dups = np.round(weights * N * p)
    duped_dataset = data_cp.loc[data_cp.index.repeat(dups)]
    duped_dataset = duped_dataset.reset_index(drop=True)
    model_init, X_train, y_train = preprocess_dataset(duped_dataset)

    # print(" --- first dataset: ---")
    # print(duped_dataset.columns)
    # print("--- first x train --- ")
    # print(X_train.columns)
    # print(" --- effect on original ---")
    # print(data.columns)

    model_init.fit(X_train, y_train)

    X, y = data.iloc[:,:-1].values, data.iloc[:,-1].values
    h = data.columns[:-1]
    X = pd.DataFrame(X, columns=h)

    X_hat = model_init.predict(X)
    correct = y == X_hat
    new_weights = sample_two_gamma_dists(correct, 2, 4)
    init_loss = calc_weighted_loss(correct, new_weights)
    print(f"--- init loss: {init_loss} ---")

    w_total = sum(new_weights)
    w_norm = new_weights.copy()
    w_norm = w_norm/w_total

    data_new = data.copy(deep=True)
    dups_new = np.round(w_norm * N * p)
    duped_dataset_new = data_new.loc[data_new.index.repeat(dups_new)]
    duped_dataset_new = duped_dataset_new.reset_index(drop=True)

    new_model, X_train_new, y_train_new = preprocess_dataset(duped_dataset_new)

    # print(" --- next dataset: ---")
    # print(duped_dataset_new.columns)
    # print(" --- X_train new ---")
    # print(X_train_new.columns)

    new_model.fit(X_train_new, y_train_new)
    X_hat = new_model.predict(X)
    correct = y == X_hat
    refit_loss = calc_weighted_loss(correct, new_weights)
    print(f"--- after loss: {refit_loss} -- ")
    return init_loss/w_total, refit_loss/w_total


# Compare tree without weights to one with weights
def no_weights_vs_weighted(data, weights, p, max_depth=1, n_est=40):
    data_cp_init = data.copy(deep=True)
    N = data.shape[0]
    model_init, X_train_init, y_train_init, thresholds_init = preprocess_dataset(data_cp_init, 
                                                                                 n_est, max_depth)

    model_init.fit(X_train_init, y_train_init)
    eval_data_copy = data.copy()
    X_eval, y_eval = eval_data_copy.iloc[:,:-1].values, eval_data_copy.iloc[:,-1].values
    h_eval = eval_data_copy.columns[:-1]
    X_eval = pd.DataFrame(X_eval, columns=h_eval)
    # print('-----------------')
    # print(X_eval.columns)
    X_eval = cut(X_eval, thresholds_init)
    print(X_eval.columns)
    print(X_eval)

    X_hat_init = model_init.predict(X_eval)
    correct_init = y_eval == X_hat_init
    correct_init_cp = correct_init.copy()
    init_wloss = calc_weighted_loss(correct_init_cp, weights)
    correct_init_cp = correct_init.copy()
    init_acc = correct_init_cp.sum()/correct_init_cp.shape[0]

    # ----------------------------------------------------------
    # Weighted Tree
    # ----------------------------------------------------------

    data_cp = data.copy(deep=True)
    dups = np.round(weights * N * p)
    duped_dataset = data_cp.loc[data_cp.index.repeat(dups)]
    duped_dataset = duped_dataset.reset_index(drop=True)
    model_weighted, X_train_weighted, y_train_weighted, threshold_weighted = preprocess_dataset(duped_dataset, 
                                                                                             n_est, max_depth)

    print('-----------------')
    print(X_train_weighted.columns)
    model_weighted.fit(X_train_weighted, y_train_weighted)
    eval_data_copy_2 = data.copy()
    X_eval_2, y_eval_2 = eval_data_copy_2.iloc[:,:-1].values, eval_data_copy_2.iloc[:,-1].values
    h_eval_2 = eval_data_copy_2.columns[:-1]
    X_eval_2 = pd.DataFrame(X_eval_2, columns=h_eval_2)

    # print('-----------------')
    # print(X_eval.columns)
    X_eval_2 = cut(X_eval_2, threshold_weighted)
    # print(X_eval.columns)
    # print(X_eval)

    X_hat_weighted = model_weighted.predict(X_eval_2)
    correct_weighted = y_eval_2 == X_hat_weighted
    correct_weighted_cp = correct_weighted.copy()
    weighted_wloss = calc_weighted_loss(correct_weighted_cp, weights)
    correct_weighted_cp = correct_weighted.copy()
    weighted_acc = correct_weighted_cp.sum()/correct_weighted_cp.shape[0]

    w_total = sum(weights)

    print(f"({init_wloss/w_total}, {init_acc}, {weighted_wloss/w_total}, {weighted_acc})")
    return init_wloss/w_total, init_acc, weighted_wloss/w_total, weighted_acc  


def resample_and_compare_baseline(data, weights):
    n_est, max_depth = 40, 1
    init_loss, model, thresholds = perform_tree_fitting(data, data, weights, n_est=n_est, max_depth=max_depth, \
                                                        return_model=True, return_thresholds=True)
    print('Train loss:', init_loss)
    # predict
    X, y = apply_thresholds(data, thresholds, n_est, max_depth)
    y_hat = model.predict(X)
    correct = y.to_numpy().reshape(-1) == y_hat
    # sample weights according to incorrectly predicted
    new_weights = sample_two_gamma_dists(correct, 1, 100_000)
    new_weights = new_weights / new_weights.sum()
    new_weight_loss = weighted_loss(model, X, y, X, y, new_weights)
    print('Train loss new weighting:', new_weight_loss)
    new_weight_trained_loss = mathiasSampling(data, new_weights, 10)
    print('Train loss model on new weighting', new_weight_trained_loss)
    return init_loss - new_weight_trained_loss


def baseline(data, weights):
    return perform_tree_fitting(data, data, weights)


def gosdtDeterministic(data, weights, p):
    N = data.shape[0]
    dups = np.round(weights * N * p)
    duped_dataset = data.loc[data.index.repeat(dups)]
    # print(duped_dataset.shape[0], N * p)
    duped_dataset = duped_dataset.reset_index(drop=True)
    return perform_tree_fitting(duped_dataset, data, weights)


def gosdtSampling(data, weights, p):
    N = data.shape[0]
    sampled_data = data.sample(n=int(N * p), replace=True, weights=weights, ignore_index=True)
    return perform_tree_fitting(sampled_data, data, weights)


def mathiasSampling(data, weights, p):
    N = data.shape[0]
    deter_count = np.floor(weights * N * p) # determinisitc part of duplication
    # print("disc\n", deter_count[:5])
    # print("p\n", (weights*N*p - deter_count)[:5])
    stoch_count = (np.random.rand(weights.shape[0]) < (weights * N * p - deter_count)).astype(int) # stochastic part
    # print("stoch\n", stoch_count[:5])
    sampled_dups = deter_count + stoch_count # combine to get the samples that should be duplicated
    # print("dups\n", sampled_dups[:5])
    duped_dataset = data.loc[data.index.repeat(sampled_dups)]
    duped_dataset = duped_dataset.reset_index(drop=True)
    return perform_tree_fitting(duped_dataset, data, weights)


def sample_weights(dist, N, *kwargs):
    if dist == 'exponential':
        return random.exponential(scale=1/float(kwargs[0]), size=N)
    else:
        raise RuntimeError(f'Distribution of type {dist} cannot be handled')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--weight_dist', type=str, choices=WEIGHTING_TYPES, help='Weighting distribution')
    parser.add_argument('--weight_args', nargs='*')
    parser.add_argument('--sampling_method', type=str, choices=SAMPLE_TYPES, help='Type of sampling method')
    parser.add_argument('-p', type=float, help='Dataset multiplier')
    parser.add_argument('--out', type=str, help='Where results are written to')
    args = parser.parse_args()

    # Load dataset
    data = pd.read_csv('datasets/fico.csv')
    N = data.shape[0]

    # Preporcess dataset and get model
    # model, X_train, y_train = preprocess_dataset(data)
    # data = pd.concat((X_train, y_train), axis=1)
    model = None
    # Sample weights from distribution
    weights = sample_weights(args.weight_dist, N, *args.weight_args)
    weights = weights / weights.sum() # Normalize weights
    
    # Dup dataset and fit model
    print(f'Weight distribution {args.weight_dist}({", ".join(map(str, args.weight_args))}), \tp={args.p}')
    accuracy, loss, time = 0, 0, 0
    init_wLoss, init_uwLoss, init_acc, retrain_wLoss, retrain_uwLoss, retrain_acc = 0, 0, 0, 0, 0, 0
    if args.sampling_method == 'mathias':
        loss = mathiasSampling(data, weights, args.p)
    elif args.sampling_method == 'sampling':
        loss = gosdtSampling(data, weights, args.p)
    elif args.sampling_method == 'deterministic':
        loss = gosdtDeterministic(data, weights, args.p)
    elif args.sampling_method == 'baseline':
        loss = baseline(data, weights)
    elif args.sampling_method == 'resample_weight_deterministic':
        init_loss, retrain_loss = resample_and_compare_deterministic(data, weights, args.p)
    elif args.sampling_method == 'no_weights_vs_weights':
        init_loss, init_acc, weighted_loss, weighted_acc = no_weights_vs_weighted(data, weights, args.p)
    elif args.sampling_method == 'resample_weight_baseline':
        loss = resample_and_compare_baseline(data, weights)
    else:
        raise RuntimeError(f'Sampling of type {args.sampling_method} cannot be handled')
    
    two_loss_reporting = ["resample_weight_deterministic"]
    four_less_reporting = ["no_weights_vs_weights"]
    # Write to file
    if args.out is not None:
        import os.path
        add_header = not os.path.exists(args.out)
        with open(args.out, 'a+') as file:
            if add_header and args.sampling_method in two_loss_reporting:
                file.write('sampling_method,distribution,param,p,loss,loss_type\n')
            if add_header and args.sampling_method in four_less_reporting:
                file.write('sampling_method,distribution,param,p,loss,loss_type\n')
            elif add_header:
                file.write('sampling_method,distribution,p,loss\n')
            if args.sampling_method in two_loss_reporting:
                file.write(f'{args.sampling_method}, {args.weight_dist},({"".join(args.weight_args)}), {args.p}, {init_loss}, {"Initial"}\n')
                file.write(f'{args.sampling_method}, {args.weight_dist},({"".join(args.weight_args)}), {args.p}, {retrain_loss}, {"Retrained"}\n')
            elif args.sampling_method in four_less_reporting:
                file.write(f'{args.sampling_method}, {args.weight_dist},({"".join(args.weight_args)}), {args.p}, {init_loss}, {"initial_weighted_loss"}\n')
                file.write(f'{args.sampling_method}, {args.weight_dist},({"".join(args.weight_args)}), {args.p}, {init_acc}, {"initial_accuracy"}\n')
                file.write(f'{args.sampling_method}, {args.weight_dist},({"".join(args.weight_args)}), {args.p}, {weighted_loss}, {"weighted_weighted_loss"}\n')
                file.write(f'{args.sampling_method}, {args.weight_dist},({"".join(args.weight_args)}), {args.p}, {weighted_acc}, {"weighted_accuracy"}\n')
            else:
                file.write(f'{args.sampling_method}, {args.weight_dist}({",".join(map(str, args.weight_args))}), {args.p}, {loss}\n')
            file.close()
    
            





