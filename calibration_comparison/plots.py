"""Bounded operator plots made from measured summaries only."""
from pathlib import Path

import numpy as np
import pandas as pd


def plot_results(out,features,points,targets,curves,lsb,summary,config):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    available={f.name for f in font_manager.fontManager.ttflist}
    font=next((x for x in ("Times New Roman","Liberation Serif","DejaVu Serif") if x in available),"serif")
    saved=[]
    def save(fig,name):
        fig.tight_layout()
        for suffix in ("png","pdf"):
            path=Path(out)/f"{name}.{suffix}"
            fig.savefig(path,dpi=160,bbox_inches="tight");saved.append(path)
        plt.close(fig)
    with plt.rc_context({"font.family":font,"font.size":11,"axes.grid":True,"grid.alpha":.2}):
        complete=features[features.scan_complete.eq(True)] if "scan_complete" in features else features
        training=complete[complete.phase.isin(["anchor","candidate","rescue"]) & complete.q_index.eq(0)]
        anchors=training[training.phase.eq("anchor")]
        if len(anchors):
            fig,axes=plt.subplots(1,2,figsize=(11,4))
            all_b=anchors.b_v.dropna().to_numpy()*1000
            bins=np.histogram_bin_edges(all_b,bins=50) if len(all_b) else 35
            for code,g in anchors.groupby("lsb_code"):
                for trim,p in g.groupby("trim"):
                    x=p.b_v.dropna()*1000
                    if len(x):
                        axes[0].hist(x,bins=bins,histtype="step",label=f"LSB {code}, trim {trim}")
                q=g.groupby("trim").b_v.agg(["median","min","max"])
                axes[1].plot(q.index,q["median"]*1000,"o-",label=f"LSB {code}")
                axes[1].fill_between(q.index,q["min"]*1000,q["max"]*1000,alpha=.12)
            axes[0].set(xlabel="Шумовой центр, мВ",ylabel="Пиксели")
            axes[1].set(xlabel="Код trim",ylabel="Шумовой центр, мВ")
            axes[0].legend(fontsize=7);axes[1].legend()
            save(fig,"01_noise_lsb_anchors")
        validation=complete[complete.phase.eq("verification")]
        if len(validation) and len(targets):
            fig,axes=plt.subplots(1,2,figsize=(11,4))
            for code,g in curves[curves.scope.eq("primary")].groupby("lsb_code") if len(curves) else []:
                for method,p in g.groupby("method"):
                    axes[0].plot(p.tolerance_v*1000,100*p.joint_pass/p.population,
                        label=f"LSB {code}, {method}")
            axes[0].set(xlabel="Допуск анализа, мВ",ylabel="Совместно в допуске, %",xscale="log")
            for method,color in (("P1","#c55e00"),("P2","#007a6f")):
                values=[];codes=[]
                for entry in summary["comparisons"]:
                    if entry["scope"]!="primary":
                        continue
                    row=next((r for r in entry.get("methods",[]) if r["method"]==method),None)
                    if row and row["p95_worst_error_mv"] is not None:
                        codes.append(entry["lsb_code"]);values.append(row["p95_worst_error_mv"])
                if codes:
                    order=np.argsort(codes)
                    axes[1].plot(np.array(codes)[order],np.array(values)[order],"o-",color=color,label=method)
            axes[1].set(xlabel="Код DAC_CMP_BIAS_LSB",ylabel="95-й процентиль ошибки полных измерений, мВ")
            axes[0].legend(fontsize=8);axes[1].legend()
            save(fig,"02_precision_and_joint_fraction")
            fig,axes=plt.subplots(2,len(config.lsb_codes),figsize=(4*len(config.lsb_codes),7),squeeze=False)
            for j,code in enumerate(sorted(config.lsb_codes)):
                values=validation[validation.lsb_code.eq(code)&validation.q_index.eq(0)]
                t=targets[(targets.window.eq("CD"))&targets.q_index.eq(0)].set_index("feature").target_v
                errors=np.abs(values[["positive_v","negative_v"]].to_numpy()-np.array([t["positive_v"],t["negative_v"]]))
                finite=errors[np.isfinite(errors)]
                limit=max(float(np.max(finite))*1050 if len(finite) else 1.,.001)
                for i,method in enumerate(("P1","P2")):
                    g=validation[(validation.lsb_code==code)&(validation.method==method)&validation.q_index.eq(0)]
                    g=g.groupby(["window","column","row"],as_index=False)[["positive_v","negative_v"]].mean()
                    if len(g):
                        t=targets[(targets.window.eq("CD"))&targets.q_index.eq(0)].set_index("feature").target_v
                        axes[i,j].scatter((g.positive_v-t["positive_v"])*1000,
                                          (g.negative_v-t["negative_v"])*1000,s=9,alpha=.5)
                    axes[i,j].axhline(0,color="gray",lw=.7);axes[i,j].axvline(0,color="gray",lw=.7)
                    axes[i,j].set(title=f"{method}, LSB={code}",xlabel="Ошибка положительного края, мВ",
                                  ylabel="Ошибка отрицательного края, мВ",xlim=(-limit,limit),ylim=(-limit,limit))
            save(fig,"03_two_polarity_residuals")
            code=config.lsb_codes[0]
            fig,axes=plt.subplots(2,3,figsize=(11,8))
            all_v=validation[validation.lsb_code.eq(code)&validation.q_index.eq(0)]
            t=targets[(targets.window.eq("CD"))&targets.q_index.eq(0)].set_index("feature").target_v
            limits={}
            for f in ("b_v","positive_v","negative_v"):
                values=np.abs(all_v[f].to_numpy()-t[f])*1000
                values=values[np.isfinite(values)]
                limits[f]=max(float(np.max(values)) if len(values) else 1.,.001)
            for i,method in enumerate(("P1","P2")):
                g=validation[(validation.lsb_code==code)&(validation.method==method)&validation.q_index.eq(0)]
                g=g.groupby(["column","row"],as_index=False)[["b_v","positive_v","negative_v"]].mean()
                t=targets[(targets.window.eq("CD"))&targets.q_index.eq(0)].set_index("feature").target_v
                for j,f in enumerate(("b_v","positive_v","negative_v")):
                    matrix=np.full((32,16),np.nan)
                    for x in g.itertuples():
                        matrix[int(x.row),int(x.column)-16]=(getattr(x,f)-t[f])*1000
                    image=axes[i,j].imshow(matrix,origin="lower",aspect="equal",extent=(15.5,31.5,-.5,31.5),
                        cmap="coolwarm",vmin=-limits[f],vmax=limits[f])
                    label={"b_v":"b","positive_v":"V50+","negative_v":"V50-"}[f]
                    axes[i,j].set(title=f"{method}: {label}, LSB {code}",xlabel="Столбец",ylabel="Строка")
                    axes[i,j].grid(False);fig.colorbar(image,ax=axes[i,j],label="Ошибка, мВ")
            save(fig,"04_matrix_residual_maps")
            fig,axes=plt.subplots(1,2,figsize=(11,4))
            for method,g in validation.groupby("method"):
                selected=g[g.lsb_code.eq(code)]
                for q,p in selected.groupby("q_index"):
                    p=p.groupby(["column","row"],as_index=False)[["half_span_v","eta"]].mean()
                    axes[0].hist(p.half_span_v.dropna()*1000,bins=30,histtype="step",label=f"{method}, REF {q}")
                    axes[1].hist(p.eta.dropna(),bins=30,histtype="step",label=f"{method}, REF {q}")
            axes[0].set(xlabel="Полуразмах двух границ, мВ",ylabel="Пиксели")
            axes[1].set(xlabel="Асимметрия относительно шумового центра",ylabel="Пиксели")
            for a in axes:a.legend(fontsize=8)
            save(fig,"05_channel_amplitude_and_asymmetry")
        if len(lsb):
            fig,axes=plt.subplots(1,2,figsize=(11,4))
            lsb=lsb.sort_values("lsb_code")
            axes[0].plot(lsb.lsb_code,lsb.median_observed_baseline_range_v*1000,"o-",label="Диапазон по измеренным trim")
            axes[1].plot(lsb.lsb_code,lsb.median_measured_adjacent_trim_step_v*1000,"o-",label="Измеренные соседние коды")
            axes[0].set(xlabel="Код DAC_CMP_BIAS_LSB",ylabel="Медианный диапазон коррекции, мВ")
            axes[1].set(xlabel="Код DAC_CMP_BIAS_LSB",ylabel="Медианный шаг trim, мВ")
            for a in axes:a.legend(fontsize=8)
            save(fig,"06_lsb_range_and_step")
        if len(points):
            chosen=points[points.phase.eq("verification")&points.lsb_code.eq(config.lsb_codes[0])&points.replicate.eq(0)]
            if chosen.empty:
                chosen=points[points.phase.eq("anchor")&points.lsb_code.eq(config.lsb_codes[0])]
                chosen=chosen[chosen.tag.eq("trim16")]
            pixels=chosen[["column","row"]].drop_duplicates().head(config.representative_pixels)
            for coord in pixels.itertuples():
                fig,axes=plt.subplots(1,len(config.ref_steps_v),figsize=(11,3.7),squeeze=False)
                p=chosen[chosen.column.eq(coord.column)&chosen.row.eq(coord.row)]
                for q,ax in enumerate(axes[0]):
                    bounds=[]
                    for method,color in (("P1","#c55e00"),("P2","#007a6f"),("training","#1c5985")):
                        m=p[p.method.eq(method)]
                        signal=m[m.kind.eq(f"q{q}_sig")].sort_values("threshold_voltage_v")
                        noise=m[m.kind.eq("noise")].sort_values("threshold_voltage_v")
                        if len(signal):
                            ax.plot(signal.threshold_voltage_v,signal.mean_count,label=method,color=color,lw=1)
                            active=signal[signal.mean_count.gt(.01*signal.mean_count.max())]
                            if len(active):
                                bounds.extend(active.threshold_voltage_v.to_list())
                        if len(noise):
                            ax.plot(noise.threshold_voltage_v,noise.mean_count,color=color,lw=.7,alpha=.4)
                    ax.set(xlabel="Порог, В",ylabel="Счет за экспозицию",yscale="symlog",ylim=(0,None),
                        title=f"Col={coord.column}, Row={coord.row}, ступень REF {config.ref_steps_v[q]*1000:g} мВ")
                    if bounds:
                        low,high=min(bounds),max(bounds)
                        pad=max(.1*(high-low),.005)
                        ax.set_xlim(low-pad,high+pad)
                    ax.legend(fontsize=8)
                save(fig,f"curves_c{coord.column}_r{coord.row}")
    return saved
