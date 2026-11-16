"""One-call, checkpointed experiment using the existing MGPD hardware adapter."""
from dataclasses import asdict
from datetime import datetime, timezone
import argparse
import hashlib
import json
import logging
from pathlib import Path
import secrets
import shutil
import sys
import time
import traceback
import webbrowser

import numpy as np
import pandas as pd

from mgpd import MGPDClient
from pixel_matrix import PIXEL_CODEC
from comparator_characterization.hardware import (
    MGPDMeasurementBackend, UpoPwmShotExecutor, build_standard_characterization_pixel_configs,
)
from comparator_characterization.models import NoiseScanSettings, get_window_spec, resolve_pixels
from comparator_characterization.pixel_masks import normalize_bad_pixel_map
from comparator_characterization.storage import atomic_write_json, file_sha256

from .acquisition import Scanner, verify_omr
from .analysis import (FEATURES, analyze_scan, freeze_targets, predict_maps,
                       select_measured_maps, trim_diagnostics)
from .config import ComparisonConfig, PROJECT_ROOT, preflight
from .storage import RunStore, load_scan, utc


def _code_digest(root):
    digest=hashlib.sha256()
    paths=list((root/"calibration_comparison").glob("*.py"))
    paths += [root/"mgpd.py",root/"EO_cfg.py",root/"pixel_matrix.py",root/"configuration.py"]
    paths += list((root/"comparator_characterization").glob("*.py"))
    for p in sorted(paths):
        digest.update(str(p.relative_to(root)).encode());digest.update(p.read_bytes())
    return digest.hexdigest()


def _map_digest(mapping):
    return hashlib.sha256(json.dumps(sorted((c,r,int(v)) for (c,r),v in mapping.items())).encode()).hexdigest()


def _base_pixels(config):
    base=build_standard_characterization_pixel_configs(digital_counting_enabled=True)
    for p,word in base.items():
        fields=PIXEL_CODEC.unpack(word)
        fields["PX_GAIN"]=config.gain_code
        base[p]=PIXEL_CODEC.pack(fields)
    return base


def _apply_trim(backend,base,pixels,window,mapping):
    spec=get_window_spec(window)
    configs={}
    for p in pixels:
        fields=PIXEL_CODEC.unpack(base[p]);fields[spec.pixel_trim_field]=int(mapping[p])
        fields["PX_TST_EN"]=0
        configs[p]=PIXEL_CODEC.pack(fields)
    current=backend.snapshot_pixel_configs(pixels)
    changed={p:w for p,w in configs.items() if w!=current[p]}
    if changed:
        backend.restore_pixel_configs(changed,commit_context="calibration candidate map")


def _frame_list(frames):
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()


def _observed(training,lsb,window,pixel):
    g=training[(training.lsb_code==lsb)&(training.window==window)
        &(training.column==pixel[0])&(training.row==pixel[1])&training.q_index.eq(0)]
    return set(g.trim.astype(int))


def _progress_tables(store,training,targets=None,maps=None):
    # Per-scan tables and SQLite already provide fine-grained checkpoints.
    # Rebuilding all accumulated diagnostics after every candidate is wasteful.
    if len(training) and maps is not None:
        store.table("training_responses.csv.gz",training)
        store.table("trim_response_diagnostics.csv",trim_diagnostics(training,targets))
    if targets is not None:
        store.table("targets.csv",targets)
    if maps is not None:
        store.table("trim_maps.csv",maps)


class Experiment:
    def __init__(self,backend,store,prepared,pixels,base,cancel_event=None):
        self.backend,self.store,self.prepared=backend,store,prepared
        self.config=prepared["config"];self.pixels=pixels;self.base=base
        self.reference=store.document.get("omr_reference")
        self.scanner=Scanner(backend,store,self.config,prepared["thresholds"],prepared["pulses"],
                             pixels,self.reference,cancel_event)
        self.training=[]
        self.validation=[]

    def reset(self,lsb,label):
        self.scanner.check_cancel()
        self.backend.shot_executor.recover_safe_state(self.backend.client)
        info=self.backend.initialize_standard_configuration(self.pixels,
            fclk_mhz=self.config.main_fclk_mhz,measurement_fclk_mhz=self.config.measurement_fclk_mhz,
            eo_overrides={"DAC_CMP_BIAS_LSB":int(lsb),"DAC_CMP_BIAS_LSB_TR":self.config.lsb_range_code},
            progress_callback=lambda message,percent: print(f"  reset {percent:.0f}%: {message}",flush=True))
        state=verify_omr(self.backend.client,self.reference)
        if self.reference is None:
            self.reference=state;self.scanner.omr_reference=state
            self.store.document["omr_reference"]=state
        self.store.event("software_initialization",label=label,lsb_code=int(lsb),details=info,omr=state)
        self.store.save()

    def scan(self,phase,lsb,window,mapping,*,tag,method="training",replicate=0,q_indices=(0,),read_pixels=None):
        _apply_trim(self.backend,self.base,self.pixels,window,mapping)
        context=dict(phase=phase,lsb_code=int(lsb),window=window,method=method,tag=tag,
            replicate=int(replicate),q_indices=list(q_indices),trim_map_sha256=_map_digest(mapping))
        features,points,context=self.scanner.measure(context,read_pixels=read_pixels)
        if features is not None:
            if phase in ("anchor","candidate","rescue"):
                self.training.append(features)
            elif phase=="verification":
                self.validation.append(features)
        return features

    def execute(self):
        windows=self.config.windows
        nominal=self.config.lsb_codes[0]
        uniform={p:16 for p in self.pixels}
        self.reset(nominal,"reference")
        references=[]
        for window in windows:
            references.append(self.scan("anchor",nominal,window,uniform,tag="trim16",
                q_indices=tuple(range(len(self.prepared["pulses"])))))
        targets=freeze_targets(_frame_list(references))
        self.store.table("targets.csv",targets)
        self.store.document["targets_frozen_before_optimization"]=True
        self.store.save()
        for lsb in self.config.lsb_codes:
            self.reset(lsb,f"anchor LSB {lsb}")
            for window in windows:
                for trim in self.config.trim_anchors:
                    if lsb==nominal and trim==16:
                        continue
                    self.scan("anchor",lsb,window,{p:trim for p in self.pixels},tag=f"trim{trim}")
                    _progress_tables(self.store,_frame_list(self.training),targets)
        all_maps=[]
        for lsb in self.config.lsb_codes:
            self.reset(lsb,f"local search LSB {lsb}")
            predicted=predict_maps(_frame_list(self.training),targets,self.pixels,windows,lsb)
            self.store.table(f"predicted_maps_lsb{lsb}.csv",predicted)
            for window in windows:
                for method in ("P1","P2"):
                    group=predicted[(predicted.window==window)&(predicted.method==method)]
                    original={(int(x.column),int(x.row)):int(x.trim) for x in group.itertuples()}
                    for offset in (0,-1,1):
                        mapping={p:max(0,min(31,c+offset)) for p,c in original.items()}
                        training=_frame_list(self.training)
                        missing=[p for p,c in mapping.items() if c not in _observed(training,lsb,window,p)]
                        if missing:
                            self.scan("candidate",lsb,window,mapping,tag=f"{method}_offset{offset}",
                                      method=method,read_pixels=missing)
                            _progress_tables(self.store,_frame_list(self.training),targets)
                # Nonmonotone observed responses get a bounded exhaustive rescue.
                training=_frame_list(self.training)
                diagnostics=trim_diagnostics(training,targets)
                d=diagnostics[(diagnostics.lsb_code==lsb)&(diagnostics.window==window)
                               &diagnostics.nonmonotone_observed]
                d=d.sort_values("slope_spread_v_per_trim",ascending=False)
                selected=[(int(x.column),int(x.row)) for x in d.head(self.config.exhaustive_pixel_budget).itertuples()]
                if len(d)>len(selected):
                    self.store.event("rescue_budget",lsb_code=lsb,window=window,
                        requested=len(d),selected=len(selected),remaining_status="bounded_search_not_exhaustive")
                for code in range(32):
                    training=_frame_list(self.training)
                    missing=[p for p in selected if code not in _observed(training,lsb,window,p)]
                    if not missing:
                        continue
                    mapping={p:(code if p in selected else 16) for p in self.pixels}
                    self.scan("rescue",lsb,window,mapping,tag=f"fulltrim{code}",read_pixels=missing)
                maps=select_measured_maps(_frame_list(self.training),targets,self.pixels,windows,lsb)
                all_maps.append(maps)
        maps=_frame_list(all_maps).drop_duplicates(["method","window","column","row","lsb_code"])
        _progress_tables(self.store,_frame_list(self.training),targets,maps)
        self.store.document["maps_frozen_before_verification"]=True
        self.store.document["methods"]={"P1":"minimax_noise_plus_positive", "P2":"minimax_noise_plus_both_polarities",
            "P0":"noise_only_offline_map_not_hardware_verified"}
        self.store.save()
        # Balanced order reduces, but cannot eliminate, time/temperature drift.
        for repeat in range(self.config.verification_repeats):
            offset=repeat%len(self.config.lsb_codes)
            visits=self.config.lsb_codes[offset:]+self.config.lsb_codes[:offset]
            for lsb in visits:
                order=("P1","P2") if repeat%2==0 else ("P2","P1")
                for method in order:
                    self.reset(lsb,f"verification {method} repeat {repeat}")
                    for window in windows:
                        g=maps[(maps.method==method)&(maps.window==window)&(maps.lsb_code==lsb)]
                        mapping={(int(x.column),int(x.row)):int(x.trim) for x in g.itertuples()}
                        self.scan("verification",lsb,window,mapping,tag="frozen_map",method=method,
                                  replicate=repeat,q_indices=tuple(range(len(self.prepared["pulses"]))))
                    self.store.table("validation_responses.csv.gz",_frame_list(self.validation))
        return targets,maps


def _run_path(results_root,resume):
    latest=results_root/"latest.json"
    if resume not in ("auto",False,None) and not isinstance(resume,bool):
        path=Path(resume).resolve()
        if not (path/"run.json").is_file():
            raise FileNotFoundError(path/"run.json")
        return path,True
    if resume=="auto" and latest.is_file():
        document=json.loads(latest.read_text(encoding="utf-8"))
        path=results_root/document["run_directory"]
        if (path/"run.json").is_file():
            status=json.loads((path/"run.json").read_text(encoding="utf-8")).get("status")
            if status!="complete":
                return path,True
    name=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"_"+secrets.token_hex(2)
    return results_root/name,False


def run_calibration_comparison(*,config=None,results_root=None,resume="auto",cancel_event=None):
    """Run the complete preset. Calling again resumes an unfinished run by default.

    Requires MGPDLab with the operator-confirmed 10 ms shutter/UPO modes.
    No VISA/scope/generator/GUI dependency and no physical RESET command.
    """
    prepared=preflight(config)
    config=prepared["config"]
    results=Path(results_root or prepared["results_root"]).resolve()
    if len(str(results))>155:
        raise ValueError("Use a shorter results_root for Windows-compatible output paths")
    results.mkdir(parents=True,exist_ok=True)
    path,resuming=_run_path(results,resume)
    document={**prepared["document"],"pixels":[list(x) for x in resolve_pixels("all")],
              "windows":list(config.windows),"pulses":list(prepared["pulses"]),
              "code_sha256":_code_digest(PROJECT_ROOT),
              "mask_sha256":file_sha256(prepared["bad_pixel_mask"]) if prepared["bad_pixel_mask"] else None,
              "charge_calibrated":False,"charge_policy":"REF_steps_only_no_precise_charge_or_ENC_inference"}
    store=RunStore(path,document,resume=resuming)
    atomic_write_json(results/"latest.json",{"run_directory":path.name})
    log_handler=logging.FileHandler(path/"experiment.log",encoding="utf-8")
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(log_handler)
    if resuming and store.document.get("acquisition_complete"):
        # A locked plot/file or interrupted report must never start another
        # hardware experiment after every acquisition has already finished.
        store.document["status"]="complete"
        store.save();store.close()
        logging.getLogger().removeHandler(log_handler);log_handler.close()
        try:
            result={"experiment_path":str(path),"status":"complete",
                    **analyze_results(path,generate_plots=config.generate_plots)}
        except BaseException as error:
            document=json.loads((path/"run.json").read_text(encoding="utf-8"))
            document.update(status="analysis_failed",last_error={"type":type(error).__name__,"message":str(error)})
            atomic_write_json(path/"run.json",document)
            raise
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
        if config.open_report and result.get("report_path"):
            webbrowser.open(Path(result["report_path"]).resolve().as_uri())
        return result
    backend=None
    pending_error=None
    started=time.perf_counter()
    print(f"{'RESUME' if resuming else 'NEW'}: {path}",flush=True)
    print("CD / 512 pixels / full 0..1023 noise at trim 0,16,31 for each LSB.",flush=True)
    try:
        inputs=path/"inputs";inputs.mkdir(exist_ok=True)
        for key,lut in {**prepared["thresholds"],**prepared["references"]}.items():
            if lut.source_path is not None and not (inputs/f"{key}.csv").exists():
                shutil.copyfile(lut.source_path,inputs/f"{key}.csv")
        pixels=resolve_pixels("all")
        base=_base_pixels(config)
        noise=NoiseScanSettings(noise_repeats=config.noise_repeats,shutter_duration_s=config.shutter_s,
            settling_time_s=0.1,counter_mode_bits=16,decode_lfsr=True,lfsr_direction="left",
            configure_get_shot_omr=False,continue_after_pixel_read_error=True,
            empty_matrix_repeats_to_skip_remaining=None)
        with MGPDClient(config.host,config.port,timeout=config.timeout_s) as client:
            backend=MGPDMeasurementBackend(client,base_pixel_configs=base,counter_key="low",
                noise_settings=noise,shot_executor=UpoPwmShotExecutor(prepared["pwm"]),
                bad_pixel_map=prepared["bad_pixel_mask"],status_callback=lambda s:print(s,flush=True))
            store.document["bad_pixels"]=[list(p) for p in backend.bad_pixels]
            store.save()
            experiment=Experiment(backend,store,prepared,pixels,base,cancel_event)
            try:
                experiment.execute()
            finally:
                operation_failed=sys.exc_info()[0] is not None
                # Device cleanup occurs before the TCP context closes, including Ctrl+C.
                if backend.safe_for_pixel_cleanup:
                    try:
                        backend.shot_executor.recover_safe_state(client)
                        backend.program_noise_pixel_configuration(pixels)
                        store.event("cleanup",ctrl_idle=True,injection_disabled=True)
                    except Exception as error:
                        store.event("cleanup_failed",error=str(error))
                        if not operation_failed:
                            raise
                else:
                    store.event("cleanup_blocked",reason="uncertain_UPO_state_or_disconnection")
        store.document["acquisition_complete"]=True
        store.document["status"]="complete"
    except BaseException as error:
        pending_error=error
        store.document["status"]="interrupted" if isinstance(error,KeyboardInterrupt) else "failed"
        store.document["last_error"]={"type":type(error).__name__,"message":str(error)}
        store.event("experiment_error",error=str(error),traceback=traceback.format_exc())
    finally:
        store.document["last_session_elapsed_s"]=time.perf_counter()-started
        store.document["last_session_end_utc"]=utc()
        store.save();store.close()
        logging.getLogger().removeHandler(log_handler);log_handler.close()
    # Offline export is also attempted on interruption; never hides the original failure.
    result={"experiment_path":str(path),"status":store.document["status"]}
    try:
        result.update(analyze_results(path,generate_plots=config.generate_plots))
    except BaseException as error:
        print(f"Offline report incomplete: {error}. Raw checkpoints are retained at {path}",flush=True)
        if pending_error is None:
            document=json.loads((path/"run.json").read_text(encoding="utf-8"))
            document.update(status="analysis_failed",last_error={"type":type(error).__name__,"message":str(error)})
            atomic_write_json(path/"run.json",document)
            raise
    if pending_error is not None:
        print(f"Run saved as {store.document['status']}. Call the same function to resume.",flush=True)
        raise pending_error
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    if config.open_report and result.get("report_path"):
        webbrowser.open(Path(result["report_path"]).resolve().as_uri())
    return result


def analyze_results(root,*,generate_plots=True,rebuild=False):
    from .export import make_report
    root=Path(root).resolve()
    document=json.loads((root/"run.json").read_text(encoding="utf-8"))
    values=dict(document["settings"])
    for key in ("windows","lsb_codes","trim_anchors","ref_steps_v","ref_code_limits"):
        values[key]=tuple(values[key])
    config=ComparisonConfig(**values)
    frames=[]
    points=[]
    from comparator_characterization.calibration import load_threshold_dac_calibrations
    thresholds=load_threshold_dac_calibrations({f"DAC_CMP_{x}":root/"inputs"/f"DAC_CMP_{x}.csv" for x in "ABCD"})
    for context in document.get("scans",[]):
        complete=context["status"]=="complete"
        sid=context["scan_id"]
        fpath=root/"analysis"/"scans"/f"{sid}_features.csv"
        ppath=root/"analysis"/"scans"/f"{sid}_points.csv.gz"
        if complete and not rebuild and fpath.is_file() and ppath.is_file():
            f=pd.read_csv(fpath)
            try:
                p=pd.read_csv(ppath)
            except pd.errors.EmptyDataError:
                p=pd.DataFrame()
        else:
            lut=thresholds[get_window_spec(context["window"]).threshold_dac]
            raw=load_scan(root,sid)
            if raw.empty:
                continue
            f,p=analyze_scan(raw,context,lut,config)
        f["scan_complete"]=complete
        frames.append(f)
        if p.empty:
            continue
        # Bounded diagnostic curve subset only; full point data stay on disk.
        selected=[tuple(x) for x in document["pixels"]][::max(1,512//config.representative_pixels)][:config.representative_pixels]
        keep=p[["column","row"]].apply(tuple,axis=1).isin(selected)
        p=p[keep].copy()
        for name in ("scan_id","phase","method","lsb_code","replicate","tag"):
            p[name]=context.get(name)
        p["scan_complete"]=complete
        points.append(p)
    return make_report(root,document,_frame_list(frames),_frame_list(points),config,generate_plots=generate_plots)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-config",action="store_true")
    parser.add_argument("--new",action="store_true",help="Start a separate run, preserving old checkpoints")
    parser.add_argument("--resume",type=Path,help="Resume this experiment directory")
    parser.add_argument("--analyze",type=Path,help="Offline analysis only; never opens hardware")
    parser.add_argument("--rebuild",action="store_true",help="Refit saved raw.sqlite during offline analysis")
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    if args.analyze:
        print(json.dumps(analyze_results(args.analyze,rebuild=args.rebuild),ensure_ascii=False,indent=2))
    elif args.check_config:
        print(json.dumps(preflight()["document"],ensure_ascii=False,indent=2))
    else:
        run_calibration_comparison(resume=args.resume or (False if args.new else "auto"))


if __name__=="__main__":
    main()
