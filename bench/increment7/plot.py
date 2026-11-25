#!/usr/bin/env python3
"""Plot measured medians/ranges from comparison.json (matplotlib==3.10.3)."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('directory',type=Path)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
data=json.loads((a.directory/'comparison.json').read_text())
scenarios=['normal','delayed','saturation','tls-warm','tls-cold','sse','slow-reader','ws']
labels=['HTTP','Backend\nlento','Saturación','TLS\ncaliente','TLS\nfrío','SSE','Lector\nlento','WebSocket']
colors={'cauce':'#156B95','nginx':'#DC722E'}
lookup={(g['scenario'],g['stack']):g for g in data['groups'] if g['logs']=='off'}
fig,axes=plt.subplots(2,2,figsize=(14,8),layout='constrained')
for ax,(metric,title,ylabel,log) in zip(axes.flat,[
 ('p95_ms','Latencia p95 de respuestas útiles','ms desde llegada programada (log)',True),
 ('goodput_rps','Throughput útil, incluido drenaje','respuestas / segundo',False),
 ('proxy_cpu_pct','CPU del proxy','% de un núcleo',False),
 ('cgroup_peak_mib','Memoria del contenedor: máximo muestreado','MiB; uso de cgroup, incluida caché',False)]):
 for j,stack in enumerate(('cauce','nginx')):
  points=[lookup[(s,stack)][metric] for s in scenarios]
  med=np.array([x['median'] for x in points]); low=med-np.array([x['min'] for x in points]);high=np.array([x['max'] for x in points])-med
  ax.bar(np.arange(len(scenarios))+(j-.5)*.36,med,width=.34,color=colors[stack],label='Cauce 0.7' if stack=='cauce' else 'NGINX 1.30.6',yerr=np.stack([low,high]),capsize=2)
 ax.set_xticks(np.arange(len(scenarios)),labels,fontsize=8)
 ax.set_title(title,loc='left',fontweight='bold',fontsize=12)
 ax.set_ylabel(ylabel,fontsize=9)
 if log:ax.set_yscale('log')
 ax.set_axisbelow(True);ax.grid(axis='y',alpha=.2)
 ax.spines[['top','right']].set_visible(False)
axes[0,0].legend(frameon=False,fontsize=9)
fig.suptitle('Cauce / NGINX — Docker ARM64, 1 CPU y 256 MiB por proxy\nLogs desactivados · mediana de 3 repeticiones · barras de error: mínimo/máximo',fontsize=14,fontweight='bold')
a.output.parent.mkdir(parents=True,exist_ok=True)
fig.savefig(a.output,dpi=180)
fig.savefig(a.output.with_suffix('.svg'))
print(a.output)
