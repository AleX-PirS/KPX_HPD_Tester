"""Pure analysis of digital count responses; no device commands."""
from dataclasses import replace
import json
import math

import numpy as np
import pandas as pd

from comparator_characterization.analysis import fit_noise_curve, _fit_scurve_group
from comparator_characterization.models import AnalysisSettings
from comparator_characterization.parallel import map_analysis_groups

FEATURES = ("b_v", "positive_v", "negative_v")
U_FIELDS = {"b_v":"b_fit_u_v", "positive_v":"positive_v50_uncertainty_v",
            "negative_v":"negative_v50_uncertainty_v"}
METHOD_FEATURES = {"P0": ("b_v",), "P1": ("b_v","positive_v"), "P2": FEATURES}


def numeric(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return math.nan


def point_table(raw, lut):
    if raw.empty:
        return pd.DataFrame()
    frame = raw.copy()
    if "acquisition_valid" in frame:
        frame=frame[frame.acquisition_valid.eq(True)].copy()
    if frame.empty:
        return pd.DataFrame()
    frame["count"] = pd.to_numeric(frame["count"],errors="coerce")
    frame["good"] = frame.valid.eq(1) & frame.saturated.eq(0) & frame["count"].ge(0)
    frame["valid_count"] = frame["count"].where(frame.good)
    if "nominal_n" not in frame:
        shot_n = {}
        for row in frame[["shot_id","result_json"]].drop_duplicates().itertuples(index=False):
            shot_n[row.shot_id] = numeric(json.loads(row.result_json).get("injections_for_analysis"))
        frame["nominal_n"] = frame.shot_id.map(shot_n)
    # A crash after background but before signal must not change the paired
    # background statistics on resume. Orphans remain untouched in raw.sqlite.
    identity=["column","row","code","repeat"]
    keep=frame.kind.eq("noise")
    for kind in frame.kind.unique():
        if not str(kind).endswith("_sig"):
            continue
        bg_kind=kind[:-4]+"_bg"
        paired=frame[frame.kind.eq(kind)][identity].merge(
            frame[frame.kind.eq(bg_kind)][identity],on=identity,validate="one_to_one")
        paired_index=set(map(tuple,paired.to_numpy()))
        keep |= frame.kind.isin([kind,bg_kind]) & frame[identity].apply(tuple,axis=1).isin(paired_index)
    frame=frame[keep].copy()
    points = frame.groupby(["column","row","trim","kind","code"],as_index=False).agg(
        mean_count=("valid_count","mean"), sem_count=("valid_count","sem"),
        repeats=("good","sum"), attempts=("good","size"), nominal_n=("nominal_n","mean"))
    points["threshold_dac_code"] = points.code
    points["threshold_voltage_v"] = [lut.dac_code_to_voltage(int(x)) for x in points.code]
    return points


def _pixel_job(job):
    group, context, lut, options = job
    first = group.iloc[0]
    base = {**context, "column":int(first.column), "row":int(first.row),
            "trim":int(first.trim), "uncertainty_kind":"descriptive_fit_not_coverage"}
    noise = group[group.kind.eq("noise")]
    noise_fit = fit_noise_curve(noise,options)
    b = numeric(noise_fit.get("center_selected_v"))
    noise_ok = noise_fit.get("fit_status")=="ok" and math.isfinite(b)
    rows = []
    for q in context["q_indices"]:
        row = {**base, "q_index":q, "b_v":b if noise_ok else math.nan,
               "b_display_v":b, "b_status":noise_fit.get("fit_status"),
               "b_r2":noise_fit.get("fit_r2"), "b_sigma_v":noise_fit.get("sigma_fit_v"),
               "b_fit_u_v":noise_fit.get("center_fit_uncertainty_v")}
        signals = group[group.kind.eq(f"q{q}_sig")].copy()
        backgrounds = group[group.kind.eq(f"q{q}_bg")][["code","mean_count","repeats","attempts"]].rename(
            columns={"mean_count":"background_count","repeats":"bg_repeats","attempts":"bg_attempts"})
        signal = signals.merge(backgrounds,on="code",how="left",validate="one_to_one")
        for name, sign in (("positive",1),("negative",-1)):
            side = signal[(signal.threshold_voltage_v-b)*sign > 0].copy()
            side["fit_valid"] = (side.repeats.eq(side.attempts) & side.repeats.gt(0)
                & side.bg_repeats.eq(side.bg_attempts) & side.bg_repeats.gt(0)
                & side.nominal_n.gt(0)
                & side.background_count.le(context["max_background_fraction"]*side.nominal_n))
            side["background_corrected_detected"] = side.mean_count
            side["injections_for_analysis"] = side.nominal_n
            fit = _fit_scurve_group(side,lut,options)
            expected = "descending" if sign>0 else "ascending"
            if fit["fit_status"]=="ok" and fit["transition_direction"]!=expected:
                fit["fit_status"]="unexpected_direction"
            okay = noise_ok and fit["fit_status"]=="ok"
            row[name+"_v"] = numeric(fit.get("v50_v")) if okay else math.nan
            row[name+"_status"] = fit["fit_status"] if noise_ok else "noise_center_unresolved"
            for key in ("sigma_v","v50_uncertainty_v","fit_r2","fit_points_in_transition_core",
                        "fit_upper_plateau_efficiency","fit_lower_plateau_efficiency"):
                row[name+"_"+key] = fit.get(key)
            plateau_ratio = numeric(fit.get("fit_upper_plateau_efficiency"))
            nominal = float(side.nominal_n.median()) if len(side) else math.nan
            row[name+"_plateau_count"] = plateau_ratio*nominal
            row[name+"_plateau_to_nominal"] = plateau_ratio
            row[name+"_nominal_n"] = nominal
            row[name+"_n_source"] = "nominal_FxT_plus_fitted_response_plateau_not_actual_edge_counter"
        p,n = row["positive_v"],row["negative_v"]
        row["half_span_v"] = (p-n)/2
        row["positive_amplitude_v"] = p-row["b_v"]
        row["negative_amplitude_v"] = row["b_v"]-n
        row["eta"] = (p+n-2*row["b_v"])/(p-n) if p>n else math.nan
        row["resolved"] = all(math.isfinite(numeric(row[f])) for f in FEATURES)
        row["physical_interpretation"] = "digital_response_only_counter_operator_not_independently_validated"
        row.pop("q_indices",None)
        rows.append(row)
    return rows


def analyze_scan(raw, context, lut, config):
    points = point_table(raw,lut)
    if points.empty:
        return pd.DataFrame(), points
    options = AnalysisSettings(workers=config.workers, parallel_min_groups=128,
                               gaussian_min_r2=0.70)
    groups = points.groupby(["column","row"],sort=True)
    def jobs():
        for _, group in groups:
            yield group, context, lut, options
    results = map_analysis_groups(_pixel_job,jobs,total=groups.ngroups,settings=options,label="Calibration responses")
    return pd.DataFrame([r for group in results for r in group]),points


def freeze_targets(reference):
    """Common, fixed voltage targets, not per-pixel gain compensation."""
    result = []
    for (window,q),g in reference.groupby(["window","q_index"],sort=True):
        for feature in FEATURES:
            values = pd.to_numeric(g[feature],errors="coerce").dropna()
            if values.empty:
                raise RuntimeError(f"Cannot set target: {window}, Q{q}, {feature} has no valid fits")
            result.append(dict(window=window,q_index=int(q),feature=feature,
                               target_v=float(values.median()),target_count=len(values),
                               target_source="nominal_LSB_trim16_population_median_before_optimization"))
    return pd.DataFrame(result)


def target_vector(targets,window,q,features):
    g = targets[(targets.window==window)&(targets.q_index==q)].set_index("feature")
    return np.array([g.loc[f,"target_v"] for f in features],dtype=float)


def _collapse_codes(source,features):
    """Descriptive ranking guard, never an unvalidated confidence bound."""
    source=source[np.isfinite(source[list(features)]).all(axis=1)]
    rows=[]
    for code,g in source.groupby("trim",sort=True):
        row={"trim":int(code),"guard_complete":True}
        for f in features:
            values=pd.to_numeric(g[f],errors="coerce")
            row[f]=float(values.mean())
            fit_u=pd.to_numeric(g[U_FIELDS[f]],errors="coerce") if U_FIELDS[f] in g else pd.Series(dtype=float)
            valid_u=fit_u[np.isfinite(fit_u)&fit_u.ge(0)]
            row["guard_complete"] &= len(valid_u)==len(g)
            row[f+"_rank_guard"]=(float(valid_u.max()) if len(valid_u) else 0.)+(
                float(values.std(ddof=1)) if len(values)>1 else 0.)
        rows.append(row)
    return pd.DataFrame(rows,columns=["trim","guard_complete"]+
                        [x for f in features for x in (f,f+"_rank_guard")])


def predict_maps(training,targets,pixels,windows,lsb):
    """Separate piecewise measured responses, without assuming additive trim."""
    rows = []
    for window in windows:
        pool = training[(training.window==window)&(training.lsb_code==lsb)&training.q_index.eq(0)]
        grouped={(int(c),int(r)):g for (c,r),g in pool.groupby(["column","row"])}
        for col,r in pixels:
            group = grouped.get((col,r),pool.iloc[:0]).sort_values("trim").drop_duplicates("trim",keep="last")
            for method,features in METHOD_FEATURES.items():
                valid = _collapse_codes(group,features)
                row = dict(window=window,column=col,row=r,lsb_code=int(lsb),method=method,
                           trim=16, predicted_max_error_v=math.nan, prediction_status="unresolved")
                if len(valid)>=2 and valid.trim.min()<=16<=valid.trim.max():
                    codes = np.arange(int(valid.trim.min()),int(valid.trim.max())+1)
                    values = np.stack([np.interp(codes,valid.trim,valid[f]) for f in features],axis=1)
                    guards=np.stack([np.interp(codes,valid.trim,valid[f+"_rank_guard"]) for f in features],axis=1)
                    cost = np.max(np.abs(values-target_vector(targets,window,0,features))+guards,axis=1)
                    best = min(range(len(codes)),key=lambda i:(cost[i],abs(codes[i]-16),codes[i]))
                    row.update(trim=int(codes[best]),predicted_max_error_v=float(cost[best]),
                               prediction_status="piecewise_prediction_needs_measurement")
                rows.append(row)
    return pd.DataFrame(rows)


def select_measured_maps(training,targets,pixels,windows,lsb):
    rows = []
    for window in windows:
        pool = training[(training.window==window)&(training.lsb_code==lsb)&training.q_index.eq(0)]
        grouped={(int(c),int(r)):g for (c,r),g in pool.groupby(["column","row"])}
        for col,r in pixels:
            source = grouped.get((col,r),pool.iloc[:0])
            # Repeated measurements of an identical code have equal scan weight.
            for method,features in METHOD_FEATURES.items():
                valid = _collapse_codes(source,features)
                measured_codes=int(source.trim.nunique())
                row = dict(window=window,column=col,row=r,lsb_code=int(lsb),method=method,
                           trim=16, measured_max_error_v=math.nan, search_status="unresolved",
                           measured_codes=measured_codes, exhaustive=measured_codes==32,
                           ranking_guard_kind="max_fit_u_plus_between_scan_sd_descriptive_no_coverage",
                           guard_complete=False)
                if not valid.empty:
                    errors=np.abs(valid[list(features)].to_numpy()-target_vector(targets,window,0,features))
                    guards=valid[[f+"_rank_guard" for f in features]].to_numpy(dtype=float)
                    valid["cost"] = np.max(errors+guards,axis=1)
                    valid["max_error"] = np.max(errors,axis=1)
                    valid["distance"] = (valid.trim-16).abs()
                    best = valid.sort_values(["cost","distance","trim"]).iloc[0]
                    row.update(trim=int(best.trim),measured_max_error_v=float(best.max_error),
                               rank_score_v=float(best.cost),guard_complete=bool(best.guard_complete),
                               search_status="best_measured_code")
                rows.append(row)
    return pd.DataFrame(rows)


def trim_diagnostics(training,targets):
    rows = []
    g0 = training[training.q_index.eq(0)]
    for keys,g in g0.groupby(["lsb_code","window","column","row"],sort=True):
        g = g.groupby("trim",as_index=False)[list(FEATURES)].mean().sort_values("trim")
        row = dict(zip(("lsb_code","window","column","row"),keys))
        nonmonotone = False
        for f in FEATURES:
            valid = g.dropna(subset=[f])
            if len(valid)<2:
                row[f+"_slope_v_per_trim"] = math.nan
                continue
            slopes = np.diff(valid[f])/np.diff(valid.trim)
            row[f+"_slope_v_per_trim"] = float(np.polyfit(valid.trim,valid[f],1)[0])
            row[f+"_slope_min"] = float(np.min(slopes))
            row[f+"_slope_max"] = float(np.max(slopes))
            nonmonotone |= bool(np.min(slopes)*np.max(slopes)<0)
        slopes = [row.get(f+"_slope_v_per_trim",math.nan) for f in FEATURES]
        row["slope_spread_v_per_trim"] = float(np.ptp(slopes))
        valid = g.dropna(subset=list(FEATURES))
        row["nonmonotone_observed"] = nonmonotone
        row["d_span_v"] = float(np.ptp((valid.positive_v-valid.negative_v)/2)) if len(valid) else math.nan
        row["measured_code_count"] = len(g)
        row["exhaustive"] = len(g)==32
        rows.append(row)
    return pd.DataFrame(rows)


def compare_validation(validation,targets,pixels,windows,required_repeats=3):
    """Fixed-denominator tolerance sweep and classification on identical pixels.

    These are descriptive repeat-average classifications, not confidence-based
    certification. Missing any selected window/Q keeps a pixel out of PASS.
    """
    if validation.empty:
        return {}, pd.DataFrame(),pd.DataFrame(),pd.DataFrame()
    keys=["method","window","column","row","q_index"]
    means = validation.groupby(keys,as_index=False).agg(**{
        f:(f,"mean") for f in FEATURES}, **{f+"_sd":(f,"std") for f in FEATURES},
        **{f+"_n":(f,lambda s:int(np.isfinite(s).sum())) for f in FEATURES},
        repeats=("replicate","nunique"), resolved_repeats=("resolved","sum"))
    for f in FEATURES:
        lookup={(x.window,int(x.q_index)):float(x.target_v) for x in targets[targets.feature==f].itertuples()}
        means[f+"_error"] = means[f]-[lookup[(w,int(q))] for w,q in zip(means.window,means.q_index)]
    error = means[[f+"_error" for f in FEATURES]].to_numpy()
    means["max_error_v"] = np.max(np.abs(error),axis=1)
    # All Q values and all observed window means are required for each pixel.
    q_values=sorted(targets.q_index.unique())
    expected=len(windows)*len(q_values)
    summary,curves,confusion=[],[],[]
    grid=np.geomspace(0.00025,0.032,43)
    for method in ("P1","P2"):
        g=means[means.method==method]
        pixel_rows=[]
        for col,r in pixels:
            p=g[(g.column==col)&(g.row==r)]
            complete=(len(p)==expected and p.resolved_repeats.eq(required_repeats).all()
                      and p.repeats.eq(required_repeats).all() and np.isfinite(p.max_error_v).all())
            reduced_complete=(len(p)==expected and p.repeats.eq(required_repeats).all()
                and p.b_v_n.eq(required_repeats).all() and p.positive_v_n.eq(required_repeats).all()
                and np.isfinite(p[["b_v_error","positive_v_error"]].to_numpy()).all())
            reduced=(np.max(np.abs(p[["b_v_error","positive_v_error"]].to_numpy()))
                     if reduced_complete else math.inf)
            full=float(p.max_error_v.max()) if complete else math.inf
            pixel_rows.append((complete,reduced_complete,reduced,full))
        values=np.array([x[3] for x in pixel_rows]);finite=values[np.isfinite(values)]
        summary.append(dict(method=method,population=len(pixels),complete_pixels=int(np.isfinite(values).sum()),
            median_worst_error_mv=float(np.median(finite)*1000) if len(finite) else None,
            p95_worst_error_mv=float(np.quantile(finite,.95)*1000) if len(finite) else None))
        for tol in grid:
            reduced=np.array([x[2]<=tol for x in pixel_rows]);full=values<=tol
            known=np.array([x[0] and x[1] for x in pixel_rows])
            counts=dict(TP=int(np.sum(known&reduced&full)),FP=int(np.sum(known&reduced&~full)),
                        FN=int(np.sum(known&~reduced&full)),TN=int(np.sum(known&~reduced&~full)),
                        unknown=int(np.sum(~known)))
            curves.append(dict(method=method,tolerance_v=float(tol),population=len(pixels),
                joint_pass=int(full.sum()),positive_noise_pass=int(reduced.sum()),
                additional_violations=int(np.sum(known&reduced&~full)),
                reduced_pass_full_unresolved=int(np.sum(reduced&~known)),**counts))
            confusion.append(dict(method=method,tolerance_v=float(tol),**counts,
                comparison="same_frozen_map_reduced_vs_full_measured_mean_not_ground_truth"))
    return {"methods":summary,"classification_kind":"descriptive_repeat_means_tolerance_sweep",
            "counter_operator_validated":False,"physical_pass_claim":False},means,pd.DataFrame(curves),pd.DataFrame(confusion)
