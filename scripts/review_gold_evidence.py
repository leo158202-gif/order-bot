"""清待核前读取当前金标，逐格核对整单 B:H；台账声明不代替内容证据。"""
from pathlib import Path
import hashlib

import openpyxl


def business_rows(grid, row):
    if not 1 < row <= len(grid) or not str(grid[row-1][0]).isdigit():
        raise ValueError('必须指向订单首行')
    rows=[]
    for values in grid[row-1:]:
        if rows and str(values[0]).isdigit():
            break
        cells=[str(v).strip() if v is not None else '' for v in values[1:8]]
        if any(cells):rows.append(cells)
    return rows


def verify_gold(grid, row, proof, path):
    path=Path(path)
    if not proof or proof.get('sha256')!=hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError('缺少当前金标准文件证据')
    wb=openpyxl.load_workbook(path,read_only=True,data_only=True)
    try:
        if proof.get('sheet') not in wb.sheetnames:raise ValueError('金标准日期不存在')
        rows=[list(r[:8]) for r in wb[proof['sheet']].iter_rows(values_only=True)]
    finally:wb.close()
    hits=[i for i,r in enumerate(rows,1) if i>1 and str(r[0])==str(proof.get('seq'))]
    if len(hits)!=1:raise ValueError('金标准序号不能唯一定位')
    expected=business_rows(rows,hits[0])
    if business_rows(grid,row)!=expected:
        raise ValueError('整单产品、联系人或物流与金标准不一致，不能清待核')
    for candidate in (grid,rows):
        identical=[i for i,r in enumerate(candidate,1) if i>1 and str(r[0]).isdigit()
                   and business_rows(candidate,i)==expected]
        if len(identical)!=1:raise ValueError('相同内容有多条记录，不能证明一对一')
    return True
