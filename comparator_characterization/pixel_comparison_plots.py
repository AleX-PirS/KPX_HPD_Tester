"""Same physical pixels across comparator windows and signed inactive noise changes."""
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from .plots import _save_figure, _scurve_plot_code_window, _truthy_series, _parallel_figures


def _table(directory, name):
    path = Path(directory)/name
    try:
        return pd.read_csv(path) if path.exists() else pd.DataFrame()
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _pixel(frame, coordinate):
    if frame.empty or not {'column','row'}.issubset(frame):
        return pd.DataFrame()
    return frame[(frame.column==coordinate[0])&(frame.row==coordinate[1])].copy()


@_parallel_figures
def plot_window_pixels(analysis_paths, *, directory, settings):
    windows = ('AB','BC','CD')
    comparators = {'AB':'CMP_B','BC':'CMP_C','CD':'CMP_D'}
    tables = {w:{kind:_table(analysis_paths[w],name) for kind,name in (
        ('noise','noise_statistics.csv'),('fits','noise_fit_results.csv'),('scurve','scurve_results.csv'))} for w in windows}
    coordinates = tuple(settings.plot_pixels)
    if not coordinates:
        candidates = set()
        for data in tables.values():
            for frame in data.values():
                if {'column','row'}.issubset(frame):
                    candidates.update(zip(frame.column.astype(int),frame.row.astype(int)))
        candidates = sorted(candidates)
        indexes = np.linspace(0,len(candidates)-1,min(len(candidates),settings.representative_pixels),dtype=int)
        coordinates = tuple(candidates[i] for i in indexes) if candidates else ()
    for w in windows:
        path = Path(analysis_paths[w])/'scurve_efficiency.csv'
        parts = []
        if path.exists() and path.stat().st_size>2:
            for chunk in pd.read_csv(path,chunksize=100_000):
                use = pd.MultiIndex.from_frame(chunk[['column','row']]).isin(coordinates)
                if use.any():
                    parts.append(chunk[use])
        tables[w]['points'] = pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()
    output, directory = {}, Path(directory)
    for coordinate in coordinates:
        stem = f'pixel_c{coordinate[0]:02d}_r{coordinate[1]:02d}'
        pixels = {w:{key:_pixel(value,coordinate) for key,value in tables[w].items()} for w in windows}
        for kind in ('trim','noise'):
            fig, axes = plt.subplots(1,3,figsize=(15,4.7),layout='constrained')
            any_data = False
            for w,ax in zip(windows,axes):
                frame = pixels[w]['fits' if kind=='trim' else 'noise']
                ax.set_title(f'{w} / {comparators[w]}')
                if not frame.empty:
                    if kind=='trim':
                        subset = frame[frame.stage.astype(str).str.startswith('trim_')]
                        transfer = subset.groupby('local_trim_code').center_selected_v.median().dropna()
                        if len(transfer):
                            ax.plot(transfer.index,transfer.values*1000,'o-',label='Trim transfer');any_data=True
                        final = frame[frame.stage=='equalized_final']
                        if not final.empty:
                            ax.scatter(final.local_trim_code,final.center_selected_v*1000,marker='*',s=100,color='C3',label='equalized_final',zorder=5);any_data=True
                    else:
                        for stage in (
                            'trim_00','trim_16','trim_31',
                            'equalized_final','baseline_noise'
                        ):
                            curve = frame[frame.stage==stage].sort_values('threshold_voltage_v')
                            if not curve.empty:
                                ax.plot(curve.threshold_voltage_v*1000,curve.mean_count,'.-',ms=2,label=stage);any_data=True
                        ax.set_yscale('symlog',linthresh=1);ax.set_ylim(bottom=0)
                if not ax.lines and not ax.collections:
                    ax.text(.5,.5,'No data',ha='center',transform=ax.transAxes)
                else:
                    ax.legend(fontsize=8)
                ax.set_xlabel('Trim code' if kind=='trim' else 'Threshold voltage, mV')
                ax.set_ylabel('Effective baseline, mV' if kind=='trim' else 'Decoded counter value')
                ax.grid(alpha=.2)
            fig.suptitle(f'Pixel C{coordinate[0]:02d} R{coordinate[1]:02d}: '+('Trim transfer' if kind=='trim' else 'measured noise curves'))
            if any_data:
                key=f'{stem}_{kind}_all_windows';output[key]=_save_figure(fig,directory,key,settings)
            else:plt.close(fig)
        conditions = set()
        for data in pixels.values():
            points = data['points']
            if not points.empty:
                conditions.update((str(p),float(c) if pd.notna(c) else -1.) for p,c in points[['injection_pattern','measurement_fclk_mhz']].drop_duplicates().itertuples(index=False,name=None))
        for pattern,clock in sorted(conditions):
            if settings.plot_injection_patterns and pattern not in settings.plot_injection_patterns:continue
            suffix=f'{pattern}_fclk_{clock:g}'
            fig,axes=plt.subplots(1,3,figsize=(15,4.7),layout='constrained')
            for w,ax in zip(windows,axes):
                points=pixels[w]['points'];ax.set_title(f'{w} / {comparators[w]}')
                if not points.empty:
                    points=points[(points.injection_pattern==pattern)&(pd.to_numeric(points.measurement_fclk_mhz,errors='coerce').fillna(-1)==clock)]
                    points=points[_truthy_series(points,'active_injection_pixel_bool')&_truthy_series(points,'signal_valid')]
                    for _,curve in points.groupby('stage'):
                        limits=_scurve_plot_code_window(curve,settings)
                        if limits is not None:curve=curve[curve.threshold_dac_code.between(*limits)]
                        line=curve.groupby('threshold_voltage_v').signal_count.mean().sort_index()
                        amplitude=pd.to_numeric(curve.injection_voltage_step_v,errors='coerce').median()
                        ax.plot(line.index*1000,line.values,'.-',ms=2,label=f'{amplitude*1000:g} mV')
                if not ax.lines:ax.text(.5,.5,'No data',ha='center',transform=ax.transAxes)
                else:ax.legend(fontsize=8)
                ax.set_yscale('symlog',linthresh=1);ax.set_ylim(bottom=0)
                ax.set_xlabel('Threshold voltage, mV');ax.set_ylabel('Signal counts');ax.grid(alpha=.2)
            fig.suptitle(f'Pixel C{coordinate[0]:02d} R{coordinate[1]:02d}: S-curves, {pattern}, {clock:g} MHz')
            key=f'{stem}_scurve_{suffix}_all_windows';output[key]=_save_figure(fig,directory,key,settings)
            fig,ax=plt.subplots(figsize=(7,5),layout='constrained')
            for w in windows:
                frame=pixels[w]['scurve']
                if frame.empty:continue
                good=frame[(frame.fit_status=='ok')&(frame.injection_pattern==pattern)&(pd.to_numeric(frame.measurement_fclk_mhz,errors='coerce').fillna(-1)==clock)].sort_values('injection_charge_electrons')
                if not good.empty:ax.plot(good.injection_charge_electrons/1000,good.v50_v*1000,'o-',label=f'{w} / {comparators[w]}')
            if ax.lines:
                ax.set(xlabel='Nominal injected charge, ke',ylabel='V50, mV',title=f'Pixel C{coordinate[0]:02d} R{coordinate[1]:02d}: amplitude response, {pattern}, {clock:g} MHz')
                ax.legend();ax.grid(alpha=.2)
                key=f'{stem}_amplitude_{suffix}_all_windows';output[key]=_save_figure(fig,directory,key,settings)
            else:plt.close(fig)
    return output


def inactive_noise_statistics(efficiency):
    if efficiency.empty:return pd.DataFrame()
    use=(~_truthy_series(efficiency,'active_injection_pixel_bool')&_truthy_series(efficiency,'signal_valid')&_truthy_series(efficiency,'background_valid')&~_truthy_series(efficiency,'signal_counter_saturated')&~_truthy_series(efficiency,'background_counter_saturated'))
    data=efficiency[use&efficiency.tile_mode.eq('tile_crosstalk')].copy()
    if data.empty:return pd.DataFrame()
    data['delta_count']=data.signal_count-data.background_count
    data['delta_rate_hz']=data.delta_count/data.signal_shutter_duration_s
    keys=['stage','measurement_fclk_mhz','injection_pattern','injection_group_id','threshold_dac_code','column','row']
    result=data.groupby(keys,dropna=False).agg(repeat_count=('delta_count','count'),signal_mean_count=('signal_count','mean'),background_mean_count=('background_count','mean'),delta_mean_count=('delta_count','mean'),delta_std_count=('delta_count','std'),delta_rate_hz=('delta_rate_hz','mean'),delta_q90_count=('delta_count',lambda x:x.quantile(.90)),delta_q95_count=('delta_count',lambda x:x.quantile(.95))).reset_index()
    result['delta_sem_count']=result.delta_std_count/np.sqrt(result.repeat_count)
    return result


@_parallel_figures
def plot_inactive_noise(statistics, *, directory, settings):
    output={}
    if statistics.empty:return output
    for stage,group in statistics.groupby('stage'):
        curve=group.groupby('threshold_dac_code').delta_mean_count.agg(median='median',q10=lambda x:x.quantile(.1),q90=lambda x:x.quantile(.9))
        fig,ax=plt.subplots(figsize=(8,5),layout='constrained')
        ax.plot(curve.index,curve['median'],label='Median');ax.fill_between(curve.index,curve.q10,curve.q90,alpha=.2,label='10-90% pixels')
        ax.axhline(0,color='gray',lw=.8);ax.set(xlabel='Threshold DAC code',ylabel='Signal - background, counts',title='Inactive noise change: '+str(stage));ax.legend();ax.grid(alpha=.2)
        key='inactive_noise_'+str(stage);output[key]=_save_figure(fig,Path(directory),key,settings)
    return output
