"""Sequential adaptive shots; CPU work is parallelized only during offline analysis."""
from dataclasses import replace
import json
import math
import time
import numpy as np
import pandas as pd
from .hardware import ShotRequest
from .measurement import _acquire_point, ScurveScanRun
from .storage import ExperimentStore, utc_now_text


def noise_reference_band(statistics, pixels, settings):
    """Protect the measured noise interval without subtracting historical counts."""
    if statistics is None or statistics.empty:
        return None
    data = statistics.copy()
    for stage in ('equalized_final', 'baseline_noise', 'trim_16', 'trim_00'):
        selected = data[data.stage == stage]
        if not selected.empty:
            data = selected
            break
    chosen = set(pixels)
    data = data[[(int(c), int(r)) in chosen for c, r in zip(data.column, data.row)]]
    count = pd.to_numeric(data.mean_count, errors='coerce')
    duration = pd.to_numeric(data.shutter_duration_s, errors='coerce')
    scaled = count / duration * settings.shutter_duration_s
    significant = data[scaled > max(1.0, settings.n_injections * settings.max_background_fraction)]
    if significant.empty:
        return None
    margin = max(settings.fine_margin_codes, settings.coarse_step)
    return int(significant.threshold_dac_code.min())-margin, int(significant.threshold_dac_code.max())+margin


def acquire_adaptive_scurve(*, backend, store, calibration, spec, pixels, trim_map,
        stage, scan_phase, codes, pulse_amplitude, pulse_amplitude_configuration,
        gain_map, injection_group, upper_non_limiting_code, noise_settings,
        scurve_settings, measurement_fclk_mhz=None, noise_statistics=None,
        gain_sweep_code=None):
    settings = scurve_settings
    settings.validate()
    pixels = backend.active_pixels(pixels)
    active = tuple(p for p in injection_group.active_pixels if p in set(pixels))
    if not active:
        raise ValueError('injection group has no unmasked pixels')
    group = replace(injection_group, active_pixels=active)
    programmed_trim = backend.program_trim_map(spec, pixels, trim_map, commit=False)
    rows = backend.program_scurve_pixel_configuration(pixels, gain_map=gain_map,
        active_injection_pixels=active, tile_mode=settings.tile_mode)
    configuration_directory = store.root/'inputs/scurve_pixel_configuration'
    if gain_sweep_code is not None:
        configuration_directory = configuration_directory/f'gain_{int(gain_sweep_code):02d}'
    store.write_table(configuration_directory/f'{stage}_{group.group_id}.csv', pd.DataFrame(rows))
    descending = settings.scan_descending
    direction = -1 if descending else 1
    planned = tuple(sorted(set(int(c) for c in codes), reverse=descending))
    if not planned:
        return ScurveScanRun(stage, scan_phase, (), (), 0)
    low, high = min(planned), max(planned)
    band = noise_reference_band(noise_statistics, active, settings)
    voltage_step = pulse_amplitude_configuration.get('injection_voltage_step_v')
    if voltage_step is None and isinstance(pulse_amplitude, dict):
        voltage_step = pulse_amplitude.get('voltage_step_v', pulse_amplitude.get('injection_voltage_step_v'))
    weak = (voltage_step is None or not math.isfinite(float(voltage_step))
            or abs(float(voltage_step)) < settings.weak_signal_dense_scan_below_v)
    crosstalk = settings.tile_mode == 'tile_crosstalk' and group.pattern != 'all'
    effective_n = float(settings.n_injections)
    seen_response = noise_started = False
    noise_seen_pixels = set()
    plateau_values = []
    full_plateau_streak = tail_streak = expansion_rounds = 0
    last_background_code = previous_code = last_zero_code = None
    searching_high_zero = descending
    code = planned[0]
    visited, acquired, decisions = set(), [], []
    completed, last_bucket, stop_event = 0, -1, None

    def shot(code, repeat, kind):
        nonlocal completed
        common = {'measurement_kind':'scurve', 'stage':stage, 'scan_phase':scan_phase,
            'threshold_dac_code':code, 'repeat_index':repeat, 'pulse_amplitude':pulse_amplitude,
            'injection_pattern':group.pattern, 'injection_group_id':group.group_id,
            'measurement_fclk_mhz':measurement_fclk_mhz,
            'gain_sweep_code':gain_sweep_code,
            'scurve_background_mode':settings.background_mode, 'scurve_tile_mode':settings.tile_mode}
        pair_id = ExperimentStore.acquisition_id(json.dumps(common, sort_keys=True, ensure_ascii=True))
        outcome = _acquire_point(backend=backend, store=store, calibration=calibration,
            spec=spec, pixels=pixels, trim_map=programmed_trim, upper_non_limiting_code=upper_non_limiting_code,
            gain_map=gain_map,
            descriptor={**common, 'acquisition_type':kind},
            request=ShotRequest(measurement_kind='scurve', acquisition_type=kind,
                shutter_duration_s=settings.shutter_duration_s, test_pulses=kind=='signal',
                n_injections=settings.n_injections if kind=='signal' else None,
                pulse_amplitude=pulse_amplitude if kind=='signal' else None,
                configure_get_shot_omr=noise_settings.configure_get_shot_omr,
                counter_mode_bits=noise_settings.counter_mode_bits, mode_read=noise_settings.mode_read,
                crw_mode=noise_settings.crw_mode, measurement_fclk_mhz=measurement_fclk_mhz),
            pair_id=pair_id, injection_group=group,
            injection_capacitance_f=settings.injection_capacitance_f,
            injection_capacitance_relative_uncertainty=settings.injection_capacitance_relative_uncertainty,
            pulse_amplitude_configuration=pulse_amplitude_configuration)
        completed += int(outcome.newly_saved)
        return outcome

    def active_counts(outcome):
        values = {(c,r):count for c,r,count in outcome.selected_counts_by_pixel}
        return np.array([values.get(pixel, np.nan) for pixel in active], dtype=float)

    while low <= code <= high:
        calibration.lookup(code)
        backend.set_threshold(spec, code)
        if noise_settings.settling_time_s:
            time.sleep(noise_settings.settling_time_s)
        near_noise = band is not None and band[0] <= code <= band[1]
        checkpoint = (settings.background_mode=='paired' or crosstalk or near_noise or noise_started
            or last_background_code is None or code==planned[-1]
            or abs(code-last_background_code)>=settings.sparse_background_interval_codes)
        background = shot(code, 0, 'background') if checkpoint else None
        signal = shot(code, 0, 'signal')
        counts = active_counts(signal)
        valid = np.isfinite(counts)
        all_valid = bool(valid.all())
        response = bool(np.any(counts[valid]>0))
        if all_valid and not response:
            last_zero_code, searching_high_zero = code, False
        transition = bool(np.any((counts>=.10*effective_n)&(counts<=.90*effective_n)))
        excess = bool(np.any(counts[valid]>effective_n+max(.10*effective_n,3*math.sqrt(effective_n))))
        if background is None and ((response and not seen_response) or transition or excess):
            background = shot(code, 0, 'background')
        bg = active_counts(background) if background is not None else np.full(len(active), np.nan)
        if background is not None:
            last_background_code = code
        contaminated = bool(np.any(bg>settings.max_background_fraction*effective_n))
        noise_started |= contaminated
        for pixel, value in zip(active, bg):
            if np.isfinite(value) and value>effective_n*settings.baseline_noise_count_multiplier:
                noise_seen_pixels.add(pixel)
        repeats = settings.repeats if crosstalk or not settings.adaptive_repeats or not all_valid or transition else 1
        for repeat in range(1, repeats):
            if settings.background_mode=='paired' or crosstalk:
                shot(code, repeat, 'background')
            shot(code, repeat, 'signal')
        clean_plateau = (all_valid and not contaminated and not near_noise
            and np.all(counts>=.90*effective_n) and np.all(counts<=1.10*effective_n))
        full_plateau_streak = full_plateau_streak+1 if clean_plateau else 0
        if clean_plateau:
            plateau_values.append(float(np.median(counts)))
            if len(plateau_values)>=3:
                effective_n = float(np.median(plateau_values[-8:]))
        fresh = code not in visited
        gap = abs(code-previous_code) if previous_code is not None else 1
        origin = last_zero_code if response and not seen_response and last_zero_code is not None else previous_code
        origin_gap = abs(code-origin) if origin is not None else 1
        backfill = fresh and origin_gap>1 and (response and not seen_response or transition or contaminated)
        seen_response |= response
        visited.add(code)
        if fresh:
            acquired.append(code)
        past_reference = band is None or (code<=band[0] if descending else code>=band[1])
        tail = (noise_seen_pixels and all_valid and background is not None and np.isfinite(bg).all()
            and past_reference and all(value<=effective_n*settings.baseline_noise_count_multiplier
                for pixel,value in zip(active,bg) if pixel in noise_seen_pixels))
        tail_streak = tail_streak+1 if tail and gap==1 else (1 if tail else 0)
        decisions.append({'threshold_dac_code':code,'repeats':repeats,'background_checkpoint':background is not None,
            'transition_10_90':transition,'noise_reference_band':near_noise,'noise_started':noise_started,
            'effective_plateau_count_online':effective_n,'weak_signal_dense':weak,
            'backfill_coarse_gap':backfill,'noise_tail_streak':tail_streak})
        if settings.baseline_noise_stop_enabled and not backfill and tail_streak>=settings.baseline_noise_consecutive_codes:
            stop_event = {'stage':stage,'scan_phase':scan_phase,'injection_group_id':group.group_id,
                'measurement_fclk_mhz':measurement_fclk_mhz,'stop_code':code,
                'reason':'noise_bell_descended_to_measured_plateau_N','timestamp_utc':utc_now_text()}
            break
        if (searching_high_zero and all_valid and response and code==high
                and expansion_rounds<settings.max_expand_rounds and high<calibration.max_code):
            next_code = min(calibration.max_code, high+settings.expand_codes)
            high = next_code
            expansion_rounds += 1
            seen_response = noise_started = False
            full_plateau_streak = tail_streak = 0
            noise_seen_pixels.clear()
            store.log_status(f'S-curve {stage}/{group.group_id}: расширение поиска нуля до DAC={high}')
        elif backfill:
            next_code = origin+direction
        else:
            dense = not all_valid or near_noise or noise_started or transition or seen_response and (weak or full_plateau_streak<3)
            next_code = code+direction*(1 if dense else settings.coarse_step)
            if band is not None:
                edge = band[1] if descending else band[0]
                if min(code,next_code)<edge<max(code,next_code):
                    next_code = edge
            next_code = max(low,min(high,next_code))
        progress = 100*(high-min(visited) if descending else max(visited)-low)/max(high-low,1)
        bucket = int(progress//5)
        if bucket>last_bucket:
            last_bucket = bucket
            store.log_status(f'S-curve {stage}/{group.group_id}: DAC={code}, точек {len(visited)}, background={settings.background_mode}', stage_percent=progress)
        if next_code==code:
            break
        previous_code, code = code, next_code
    store.write_table(store.root/'online'/f'scurve_sampling_{stage}_{group.group_id}.csv', pd.DataFrame(decisions))
    store.update_metadata(scurve_progress={'stage':stage,'scan_phase':scan_phase,'injection_group_id':group.group_id,
        'acquired_codes':acquired,'new_acquisitions':completed,'baseline_stop_event':stop_event})
    if stop_event:
        events = [event for event in store.metadata.get('scurve_baseline_stop_events',[])
            if (event.get('stage'),event.get('injection_group_id'))!=(stage,group.group_id)]
        store.update_metadata(scurve_baseline_stop_events=events+[stop_event])
    return ScurveScanRun(stage,scan_phase,planned,tuple(acquired),completed,stop_event)
