"""Compact scientific data package and local operator report."""
import gzip
import hashlib
import html
import json
import math
from pathlib import Path
import sqlite3
import zipfile

import numpy as np
import pandas as pd

from comparator_characterization.storage import atomic_write_json, atomic_write_table
from .analysis import FEATURES, compare_validation, trim_diagnostics


def _write(frame,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    data=frame.to_csv(index=False).encode("utf-8")
    if path.suffix==".gz":
        path.write_bytes(gzip.compress(data,mtime=0))
    else:
        path.write_bytes(data)


def independent_confusion(validation,targets,pixels,windows):
    """Replicate 0 decisions versus independent replicate 1+2 observations.

    Reference disagreement is not a ground-truth fault diagnosis. Unknown
    observations are kept separate from TN/FP and in the population denominator.
    """
    rows=[]
    for lsb,g in validation.groupby("lsb_code"):
        for method in ("P1","P2"):
            g_method=g[g.method==method]
            observations=[]
            for col,r in pixels:
                p=g_method[(g_method.column==col)&(g_method.row==r)&g_method.q_index.eq(0)]
                per=[]
                for role,selection in (("decision",p[p.replicate.eq(0)]),("reference",p[p.replicate.gt(0)])):
                    selected_features=("b_v","positive_v") if role=="decision" and method=="P1" else FEATURES
                    values=[];known=True
                    for window in windows:
                        w=selection[selection.window==window]
                        required=1 if role=="decision" else 2
                        if w.replicate.nunique()<required:
                            known=False;break
                        t=targets[(targets.window==window)&targets.q_index.eq(0)].set_index("feature").target_v
                        for f in selected_features:
                            if not np.isfinite(w[f]).all():
                                known=False;break
                            values.append(abs(float(w[f].mean())-float(t[f])))
                    per.append((known,max(values) if known and values else math.inf))
                observations.append(per)
            for tol in np.geomspace(.00025,.032,43):
                counts=dict(TP=0,FP=0,FN=0,TN=0,unknown=0)
                for decision,reference in observations:
                    if not decision[0] or not reference[0]:
                        counts["unknown"]+=1;continue
                    a,b=decision[1]<=tol,reference[1]<=tol
                    counts["TP" if a and b else "FP" if a else "FN" if b else "TN"]+=1
                rows.append(dict(lsb_code=int(lsb),method=method,tolerance_v=float(tol),
                    population=len(pixels),**counts,decision_replicates="0",reference_replicates="1,2",
                    interpretation="independent_measured_mean_disagreement_not_fault_ground_truth"))
    return pd.DataFrame(rows)


def lsb_diagnostics(training,targets,population):
    rows=[]
    for lsb,g in training[training.q_index.eq(0)].groupby("lsb_code"):
        spans=[];steps=[];covered=0;valid=0
        for (window,col,r),p in g.groupby(["window","column","row"]):
            p=p.groupby("trim",as_index=False)[list(FEATURES)].mean().sort_values("trim")
            b=p.dropna(subset=["b_v"])
            if len(b)<2:
                continue
            valid+=1
            spans.append(float(b.b_v.max()-b.b_v.min()))
            t=float(targets[(targets.window==window)&targets.q_index.eq(0)&targets.feature.eq("b_v")].target_v.iloc[0])
            covered+=int(b.b_v.min()<=t<=b.b_v.max())
            d=np.diff(b.trim)
            local=np.abs(np.diff(b.b_v))[d==1]
            steps.extend(local)
        rows.append(dict(lsb_code=int(lsb),population=population,valid_range_pixels=valid,
            baseline_target_in_observed_range=covered,
            median_observed_baseline_range_v=float(np.median(spans)) if spans else math.nan,
            median_measured_adjacent_trim_step_v=float(np.median(steps)) if steps else math.nan,
            measured_adjacent_pairs=len(steps),
            range_interpretation="observed_codes_not_proof_of_continuous_reachability"))
    return pd.DataFrame(rows)


def make_report(root,document,features,points,config,*,generate_plots=True):
    root=Path(root);out=root/"report";out.mkdir(exist_ok=True)
    compact=root/"for_chat";compact.mkdir(exist_ok=True)
    population=len(document["pixels"])
    pixels=[tuple(p) for p in document["pixels"]]
    target_path=root/"analysis"/"targets.csv"
    targets=pd.read_csv(target_path) if target_path.is_file() else pd.DataFrame()
    complete=features[features.scan_complete.eq(True)] if len(features) and "scan_complete" in features else features
    training=complete[complete.phase.isin(["anchor","candidate","rescue"])] if len(complete) else pd.DataFrame()
    validation=complete[complete.phase.eq("verification")] if len(complete) else pd.DataFrame()
    _write(features,compact/"responses.csv.gz")
    _write(points,compact/"representative_curves.csv.gz")
    _write(targets,compact/"targets.csv")
    maps_path=root/"analysis"/"trim_maps.csv"
    if maps_path.is_file():
        _write(pd.read_csv(maps_path),compact/"trim_maps.csv")
    diagnostics=trim_diagnostics(training,targets) if len(training) else pd.DataFrame()
    _write(diagnostics,compact/"trim_response_diagnostics.csv")
    lsb=lsb_diagnostics(training,targets,population) if len(training) and len(targets) else pd.DataFrame()
    _write(lsb,compact/"lsb_range_and_step.csv")
    summary={"status":document["status"],"version":document["version"],"population":population,
        "window":"CD","calibrated_comparator":"CMP_D", "methods":document.get("methods",{}),
        "gain_equalized":False,"charge_calibrated":False,"counter_operator_validated":False,
        "lsb_current_amperes_calibrated":False,"target_source":"frozen_nominal_LSB_trim16_medians",
        "calibration_objective":"equal_voltage_weight_minimax_primary_REF_step_with_descriptive_guard",
        "ranking_guard":"max_fit_u_plus_between_scan_sd_not_a_validated_error_bound",
        "test_points_are_measured_not_assumed_equal_to_1000":True,
        "certification":False,"tolerance_interpretation":"sweep_parameter_not_device_specification",
        "repeat_model":"descriptive_stationarity_not_proven", "comparisons":[]}
    means_list=[];curves_list=[];confusion_list=[]
    if len(validation) and len(targets):
        for lsb_code,v in validation.groupby("lsb_code"):
            for scope,chosen in (("primary",[0]),("all_amplitudes",sorted(targets.q_index.unique()))):
                vv=v[v.q_index.isin(chosen)]
                tt=targets[targets.q_index.isin(chosen)]
                result,means,curves,confusion=compare_validation(vv,tt,pixels,config.windows,
                                                               required_repeats=config.verification_repeats)
                summary["comparisons"].append({"lsb_code":int(lsb_code),"scope":scope,**result})
                for frame,collection in ((means,means_list),(curves,curves_list),(confusion,confusion_list)):
                    frame["lsb_code"]=int(lsb_code);frame["scope"]=scope;collection.append(frame)
    concat=lambda frames:pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()
    means,curves,confusion=map(concat,(means_list,curves_list,confusion_list))
    _write(means,compact/"verification_means.csv.gz")
    _write(curves,compact/"tolerance_sweep.csv")
    _write(confusion,compact/"same_map_diagnostic_counts.csv")
    independent=independent_confusion(validation,targets,pixels,config.windows) if len(validation) and len(targets) else pd.DataFrame()
    _write(independent,compact/"independent_confusion.csv")
    with sqlite3.connect((root/"raw.sqlite").resolve().as_uri()+"?mode=ro",uri=True) as db:
        timing=pd.read_sql_query("""SELECT a.scan_id,a.kind,COUNT(*) shots,SUM(a.elapsed_s) elapsed_s,
            (SELECT COUNT(*) FROM samples p WHERE p.shot_id IN
                (SELECT id FROM shots t WHERE t.scan_id=a.scan_id AND t.kind=a.kind)) sample_reads
            FROM shots a GROUP BY a.scan_id,a.kind""",db)
        contexts={x["scan_id"]:x for x in document.get("scans",[])}
        for field in ("phase","method","lsb_code","replicate"):
            if len(timing):
                timing[field]=[contexts[s].get(field) for s in timing.scan_id]
        provenance=[]
        for sid,kind,result in db.execute("SELECT id,kind,result_json FROM shots ORDER BY id"):
            r=json.loads(result)
            provenance.append(dict(shot_id=sid,kind=kind,nominal_n=r.get("injections_for_analysis"),
                actual_n=r.get("actual_injections"),source=r.get("injection_count_source"),
                retries=r.get("details",{}).get("upo_acquisition_attempt_count",1)))
        denominators=pd.DataFrame(provenance)
    _write(timing,compact/"timing.csv")
    if len(denominators):
        sources=denominators.groupby(["kind","nominal_n","actual_n","source","retries"],dropna=False).size().reset_index(name="shots")
        _write(sources,compact/"injection_count_sources.csv")
    summary["timing_kind"]="shared_training_plus_measured_verification_cost_not_two_independently_trained_algorithms"
    summary["complete_scans"]=sum(x["status"]=="complete" for x in document.get("scans",[]))
    summary["incomplete_scans"]=sum(x["status"]!="complete" for x in document.get("scans",[]))
    summary["shots"]=int(timing.shots.sum()) if len(timing) else 0
    summary["sample_reads"]=int(timing.sample_reads.sum()) if len(timing) else 0
    atomic_write_json(compact/"summary.json",summary)
    # Copy only the reproducible contract; local absolute path strings are omitted.
    contract={k:v for k,v in document.items() if k not in ("scans","last_error")}
    for name in ("threshold_luts","reference_luts"):
        contract[name]={k:{a:b for a,b in v.items() if a!="source_path"} for k,v in contract.get(name,{}).items()}
    contract["scans"]=[{k:v for k,v in s.items() if k not in ("q_indices",)} for s in document.get("scans",[])]
    atomic_write_json(compact/"contract.json",contract)
    input_dir=compact/"calibration";input_dir.mkdir(exist_ok=True)
    for p in (root/"inputs").glob("*.csv"):
        (input_dir/p.name).write_bytes(p.read_bytes())
    plot_paths=[]
    if generate_plots and len(features):
        from .plots import plot_results
        plot_paths=plot_results(out,features,points,targets,curves,lsb,summary,config)
    paragraphs=["Данные счета получены из сохраненных измерений. Абсолютный заряд и ток LSB не калиброваны.",
        "P1 использует шум и положительный край. P2 использует шум и оба края. GAIN одинаков для всей матрицы.",
        "Цели зафиксированы до выбора карт. Допуск на графиках является параметром анализа, а не паспортным требованием.",
        "Измерения обучения общие. Поэтому их стоимость нельзя выдавать за время двух независимых алгоритмов.",
        "Физическая интерпретация двух переходов требует отдельной проверки счетного оператора. Пролазы и корреляции окон не анализируются.",
        "Для чата отправьте analysis_for_chat.zip. Полный raw.sqlite остается локально."]
    table=pd.DataFrame([{"LSB":x["lsb_code"],"Набор":x["scope"],**m}
        for x in summary["comparisons"] for m in x.get("methods",[])])
    content="<!doctype html><html lang='ru'><meta charset='utf-8'><title>CD: сравнение калибровки</title>"
    content+="<style>body{font:17px Georgia,serif;max-width:1200px;margin:32px auto;padding:0 20px}img{width:100%;height:auto}table{border-collapse:collapse;font-size:14px}td,th{padding:6px;border:1px solid #ccc}code{font-size:14px}</style>"
    content+=f"<h1>Калибровка CD, 512 пикселей</h1><p>Статус: <b>{html.escape(document['status'])}</b>. Завершенных сканов: {summary['complete_scans']}.</p>"
    content+="".join("<p>"+html.escape(p)+"</p>" for p in paragraphs)
    if len(table):
        content+=table.to_html(index=False,float_format=lambda x:f"{x:.4g}")
    for path in plot_paths:
        if path.suffix==".png":
            content+=f"<h2>{html.escape(path.stem)}</h2><img src='{path.name}' alt='{html.escape(path.stem)}'>"
    content+="</html>"
    (out/"index.html").write_text(content,encoding="utf-8")
    archive=root/"analysis_for_chat.zip"
    def package(include_curves):
        with zipfile.ZipFile(archive.with_suffix(".tmp"),"w",zipfile.ZIP_DEFLATED) as z:
            manifest=[]
            for path in sorted(compact.rglob("*")):
                if not path.is_file() or (not include_curves and path.name=="representative_curves.csv.gz"):
                    continue
                data=path.read_bytes();name=path.relative_to(compact).as_posix()
                z.writestr(name,data)
                manifest.append(dict(path=name,bytes=len(data),sha256=hashlib.sha256(data).hexdigest()))
            z.writestr("package_manifest.json",json.dumps({"contains_raw":False,"files":manifest,
                "representative_curves_included":include_curves},indent=2))
        archive.with_suffix(".tmp").replace(archive)
    package(True)
    if archive.stat().st_size>config.analysis_archive_limit_mb*1024**2:
        package(False)
    if archive.stat().st_size>config.analysis_archive_limit_mb*1024**2:
        raise RuntimeError("Compact analysis exceeds the configured size limit; raw data remain intact")
    return {"analysis_archive":str(archive),"analysis_archive_bytes":archive.stat().st_size,
            "report_path":str(out/"index.html"),"raw_path":str(root/"raw.sqlite")}
