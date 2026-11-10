import sys,time,json,io
from pathlib import Path
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ocr_engines import warmup
from server import process
root=Path(__file__).resolve().parents[1]
start=time.perf_counter()
print({'warmup':warmup(),'seconds':time.perf_counter()-start},flush=True)
reports=[]
for kind in ('PNG','PDF'):
 raw=(root/'samples/certificate_9.png').read_bytes()
 if kind=='PDF':
  output=io.BytesIO()
  with Image.open(io.BytesIO(raw)) as image: image.convert('RGB').save(output,'PDF',resolution=150)
  raw=output.getvalue()
 for n in range(2):
  start=time.perf_counter()
  result=process(raw,'auto')
  report={'format':kind,'attempt':n+1,'seconds':round(time.perf_counter()-start,3),
          'grades':[(r['subject'],r['grade'],r['auto_verified']) for r in result['grades']],
          'average':result['average'],'ocr':result['ocr']}
  print(json.dumps(report,ensure_ascii=False),flush=True)
  reports.append(report)
  assert len(result['grades'])==6 and result['average']==4.5 and result['auto_grades_verified']
(root/'test-results/turbo.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
