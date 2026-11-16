"""Serialized acquisition with sparse readout of individual transition regions."""
from collections import defaultdict
from dataclasses import replace
import math
import time

import numpy as np

from comparator_characterization.hardware import ShotRequest
from comparator_characterization.models import get_window_spec
from .analysis import analyze_scan, point_table
from .storage import utc


def omr_state(client):
    raw = [client.read_byte(address) for address in (0x20,0x21,0x22,0x23)]
    if any(x is None for x in raw):
        raise RuntimeError("OMR readback failed")
    return {"raw":raw, "mode_read":(raw[0]>>5)&7, "mode_cnt":(raw[1]>>2)&1,
            "crw":(raw[1]>>3)&1, "window":(raw[1]>>1)&1,
            "pol_ctrl":(raw[2]>>3)&1, "pol_sw":raw[3]&1,
            "omr_pol_bit4":(raw[0]>>4)&1,"puf":(raw[2]>>4)&1}


def verify_omr(client, reference=None):
    actual = omr_state(client)
    required = {"mode_read":2,"mode_cnt":0,"crw":0,"window":1}
    if reference:
        required.update({k:reference[k] for k in ("pol_ctrl","pol_sw","omr_pol_bit4","puf")})
    wrong = {k:(actual[k],v) for k,v in required.items() if actual[k]!=v}
    if wrong:
        raise RuntimeError(f"UPO OMR mismatch (actual,required): {wrong}. Correct UPO Load settings; Python does not rewrite its OMR image.")
    return actual


def inclusive(low,high,step):
    return sorted(set(range(int(low),int(high)+1,int(step))) | {int(high)})


def noise_regions(points, pixels, low, high, margin):
    result = defaultdict(list)
    unresolved=[]
    for pixel in pixels:
        g=points[(points.column==pixel[0])&(points.row==pixel[1])&points.kind.eq("noise")]
        valid=g.dropna(subset=["mean_count"])
        peak=valid.mean_count.max() if len(valid) else 0
        active=valid[valid.mean_count.ge(max(1.,peak*.01))]
        if active.empty:
            unresolved.append(pixel)
            continue
        start=max(low,int(active.code.min())-margin)
        stop=min(high,int(active.code.max())+margin)
        for code in range(start,stop+1):
            result[code].append(pixel)
    return dict(result),unresolved


def signal_regions(points, pixels, q, low, high, baselines, margin, background_fraction):
    result=defaultdict(set)
    unresolved=set()
    for pixel in pixels:
        g=points[(points.column==pixel[0])&(points.row==pixel[1])]
        signal=g[g.kind.eq(f"q{q}_sig")].copy()
        bg=g[g.kind.eq(f"q{q}_bg")][["code","mean_count"]].rename(columns={"mean_count":"bg"})
        signal=signal.merge(bg,on="code",how="left",validate="one_to_one")
        signal=signal[signal.bg.le(background_fraction*signal.nominal_n)]
        b=baselines.get(pixel,math.nan)
        for sign in (-1,1):
            side=signal[(signal.threshold_voltage_v-b)*sign>0].dropna(subset=["mean_count"]).sort_values("code")
            plateau=side.mean_count.max() if len(side) else math.nan
            crossings=[]
            if len(side)>=2 and plateau>0:
                data=side[["code","mean_count"]].to_numpy()
                for a,z in zip(data[:-1],data[1:]):
                    if (a[1]-plateau*.5)*(z[1]-plateau*.5)<=0 and a[1]!=z[1]:
                        crossings.append((int(a[0]),int(z[0])))
            if not crossings:
                unresolved.add(pixel)
                continue
            for a,z in crossings:
                for code in range(max(low,a-margin),min(high,z+margin)+1):
                    result[code].add(pixel)
    return {c:sorted(p) for c,p in result.items()}, sorted(unresolved)


class Scanner:
    def __init__(self, backend, store, config, thresholds, pulses, pixels, omr_reference, cancel_event=None):
        self.backend,self.store,self.config=backend,store,config
        self.thresholds,self.pulses=thresholds,pulses
        self.pixels=tuple(pixels)
        self.gain_map={p:config.gain_code for p in pixels}
        self.omr_reference=omr_reference
        self.cancel_event=cancel_event
        self.shot_count=0
        self.elapsed_acquisition_s=0.

    def check_cancel(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise KeyboardInterrupt("Calibration comparison cancelled")

    def _take(self,scan_id,kind,code,repeat,pixels,spec,trims,direction,signal=False,q=None):
        self.check_cancel()
        pulse=self.pulses[q] if q is not None else None
        n,_=self.backend.shot_executor.nominal_injections(self.config.shutter_s,counter_mode_bits=16)
        request=ShotRequest(measurement_kind="scurve" if q is not None else "noise",
            acquisition_type="signal" if signal else "background",shutter_duration_s=self.config.shutter_s,
            test_pulses=signal,n_injections=n if signal else None,pulse_amplitude=pulse,
            measurement_fclk_mhz=self.config.measurement_fclk_mhz)
        t=time.perf_counter()
        started_utc=utc()
        samples,result=self.backend.acquire(tuple(pixels),request)
        elapsed=time.perf_counter()-t
        try:
            state=verify_omr(self.backend.client,self.omr_reference)
        except Exception as error:
            invalid=replace(result,details={**dict(result.details),"omr_valid":False,"omr_error":str(error)})
            self.store.shot(scan_id,kind,code,repeat,direction,request,invalid,samples,trims,elapsed,started_utc)
            raise
        result=replace(result,details={**dict(result.details),"omr_valid":True,"omr":state})
        self.store.shot(scan_id,kind,code,repeat,direction,request,result,samples,trims,elapsed,started_utc)
        self.shot_count+=1
        self.elapsed_acquisition_s+=elapsed
        if result.details.get("upo_recovery_events"):
            self.store.event("recovery", scan_id=scan_id,details=dict(result.details))

    def measure(self,context,read_pixels=None):
        spec=get_window_spec(context["window"])
        lut=self.thresholds[spec.threshold_dac]
        upper,_=self.thresholds[spec.upper_threshold_dac].select_upper_non_limiting_endpoint()
        self.backend.counter_key=spec.inferred_counter_key
        self.backend.configure_window(spec,upper)
        verify_omr(self.backend.client,self.omr_reference)
        pixels=tuple(read_pixels if read_pixels is not None else self.pixels)
        pixels=self.backend.active_pixels(pixels)
        if not pixels:
            return None,None,None
        trims=self.backend.current_trim_map(spec,pixels)
        context={**context,"q_indices":list(context.get("q_indices",[0])),
                 "max_background_fraction":self.config.max_background_fraction,
                 "population":len(self.pixels),"readout_population":len(pixels),
                 "measurement_fclk_mhz":self.config.measurement_fclk_mhz,
                 "gain_code":self.config.gain_code,"upper_code":upper}
        scan_id=self.store.begin_scan(context)
        context["scan_id"]=scan_id
        record=next(x for x in self.store.document["scans"] if x["scan_id"]==scan_id)
        if record["status"]=="complete":
            import pandas as pd
            fp=self.store.root/"analysis"/"scans"/f"{scan_id}_features.csv"
            pp=self.store.root/"analysis"/"scans"/f"{scan_id}_points.csv.gz"
            if fp.is_file() and pp.is_file():
                print(f"[{scan_id}] checkpoint complete, reuse",flush=True)
                return pd.read_csv(fp),pd.read_csv(pp),context
        print(f"[{scan_id}] {context['phase']} {context.get('method','training')} "
              f"{spec.name}, LSB={context['lsb_code']}, {len(pixels)} pixels",flush=True)
        descending=context.get("replicate",0)%2==1
        direction="descending" if descending else "ascending"
        counters=defaultdict(int)
        completed=defaultdict(int)
        saved=self.store.raw_frame(scan_id)
        backgrounds=set()
        for x in saved.itertuples():
            key="noise" if x.kind=="noise" else str(x.kind).split("_")[0]
            counters[(key,int(x.code))]=max(counters[(key,int(x.code))],int(x.repeat))
            if str(x.kind).endswith("_bg") and x.acquisition_valid:
                backgrounds.add((key,int(x.code),int(x.repeat),int(x.column),int(x.row)))
        for x in saved.itertuples():
            if not x.acquisition_valid:
                continue
            pixel=(int(x.column),int(x.row)); code=int(x.code)
            if x.kind=="noise":
                completed[("noise",code,pixel)]+=1
            elif str(x.kind).endswith("_sig"):
                key=str(x.kind).split("_")[0]
                if (key,code,int(x.repeat),*pixel) in backgrounds:
                    completed[(key,code,pixel)]+=1
        if len(saved):
            self.store.event("scan_resumed",scan_id=scan_id,saved_sample_count=len(saved))
            print(f"  resume: {len(saved)} stored samples; completed points/repeats will be skipped",flush=True)
        def point(code,chosen,kind,q=None,repeats=1):
            key="noise" if q is None else f"q{q}"
            chosen=tuple(p for p in chosen if completed[(key,code,p)]<repeats)
            if not chosen:
                return
            self.backend.set_threshold(spec,int(code))
            time.sleep(self.backend.settings.settling_time_s)
            for _ in range(repeats):
                chosen=tuple(p for p in chosen if completed[(key,code,p)]<repeats)
                if not chosen:
                    break
                counters[(key,code)]+=1
                repeat=counters[(key,code)]
                if q is None:
                    self._take(scan_id,"noise",code,repeat,chosen,spec,trims,direction)
                else:
                    self._take(scan_id,f"q{q}_bg",code,repeat,chosen,spec,trims,direction,q=q)
                    self._take(scan_id,f"q{q}_sig",code,repeat,chosen,spec,trims,direction,signal=True,q=q)
                for pixel in chosen:
                    completed[(key,code,pixel)]+=1

        def grid(plan,kind,q=None,repeats=1):
            codes=sorted(plan,reverse=descending)
            t=time.perf_counter()
            last_report=t
            for index,code in enumerate(codes,1):
                self.check_cancel()
                point(code,plan[code],kind,q,repeats)
                now=time.perf_counter()
                if index==1 or index==len(codes) or index%16==0 or now-last_report>=10:
                    elapsed=time.perf_counter()-t
                    eta=(len(codes)-index)*elapsed/index
                    print(f"  {kind}: {index}/{len(codes)} DAC points; ETA {eta/60:.1f} min; "
                          f"shots={self.shot_count}",flush=True)
                    last_report=now

        low,high=lut.min_code,lut.max_code
        coarse=inclusive(low,high,self.config.coarse_step)
        self.backend.program_noise_pixel_configuration(self.pixels)
        if self.config.anchor_full_noise and context["phase"]=="anchor":
            grid({c:pixels for c in inclusive(low,high,1)},"noise full 0..1023",repeats=self.config.noise_repeats)
        else:
            grid({c:pixels for c in coarse},"noise coarse",repeats=self.config.noise_repeats)
            points=point_table(self.store.raw_frame(scan_id),lut)
            plan,missing=noise_regions(points,pixels,low,high,self.config.coarse_step)
            if missing:
                # Broad search is never replaced with zero-filled observations.
                grid({c:missing for c in inclusive(low,high,4)},"noise rescue",repeats=self.config.noise_repeats)
                points=point_table(self.store.raw_frame(scan_id),lut)
                plan,missing=noise_regions(points,pixels,low,high,self.config.coarse_step)
                if missing:
                    grid({c:missing for c in inclusive(low,high,1)},"noise full rescue",repeats=self.config.noise_repeats)
            grid(plan,"noise fine",repeats=self.config.noise_repeats)
        raw=self.store.raw_frame(scan_id)
        # Fit the noise first without spending signal shots on fictitious centers.
        features,noise_points=analyze_scan(raw,context,lut,self.config)
        centers={(int(x.column),int(x.row)):float(x.b_display_v) for x in features.itertuples()}
        del raw,noise_points
        self.backend.program_scurve_pixel_configuration(self.pixels,gain_map=self.gain_map,
            active_injection_pixels=self.backend.active_pixels(self.pixels),tile_mode="tile_measurement")
        for q in context["q_indices"]:
            self.backend.configure_test_pulse_amplitude(self.pulses[q])
            time.sleep(self.backend.settings.settling_time_s)
            grid({c:pixels for c in coarse},f"Q{q} coarse",q=q,repeats=self.config.signal_repeats)
            points=point_table(self.store.raw_frame(scan_id),lut)
            plan,missing=signal_regions(points,pixels,q,low,high,centers,
                self.config.fine_margin_codes,self.config.max_background_fraction)
            if missing:
                grid({c:missing for c in inclusive(low,high,4)},f"Q{q} rescue",q=q,repeats=self.config.signal_repeats)
                points=point_table(self.store.raw_frame(scan_id),lut)
                plan,missing=signal_regions(points,pixels,q,low,high,centers,
                    self.config.fine_margin_codes,self.config.max_background_fraction)
            grid(plan,f"Q{q} fine",q=q,repeats=self.config.signal_repeats)
            if missing:
                self.store.event("unresolved_bracket",scan_id=scan_id,q_index=q,pixels=missing,
                                 action="retain_unresolved_no_symmetry_assumption")
        self.backend.program_noise_pixel_configuration(self.pixels)
        features,points=analyze_scan(self.store.raw_frame(scan_id),context,lut,self.config)
        self.store.table(f"scans/{scan_id}_features.csv",features)
        self.store.table(f"scans/{scan_id}_points.csv.gz",points)
        self.store.finish_scan(scan_id)
        return features,points,context
