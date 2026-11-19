import csv,io,json
from fastapi import HTTPException
def _stream(rows):
 if not rows: raise HTTPException(409,'尚无结果')
 stream=io.StringIO();keys=list(dict.fromkeys(k for row in rows for k in row));w=csv.DictWriter(stream,fieldnames=keys);w.writeheader()
 for row in rows: w.writerow({k:(json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v) for k,v in row.items()})
 return '\ufeff'+stream.getvalue()
def csv_text(result):
 return _stream(result.get('rows',[]))
def occupancy_csv(result):
 # 仅导出已发布的日期×小组整数分摊明细；重叠段与逐笔追溯由各自端点分页导出，避免巨型JSON
 return _stream(result.get('occupancy',{}).get('apportion',[]))
def rows_csv(rows):
 return _stream(rows)
