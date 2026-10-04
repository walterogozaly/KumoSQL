"""Introduce a grouped SUM only when the selected join has at most one row.

A non-NULL literal fixes every member of a declared key of the left input.
The right input is a plain grouped COUNT(*) and every grouping key is equated
to a left column. Thus it matches at most one group. A grouped SUM on this
zero-or-one-row relation preserves both empty cardinality and NULL values;
the existing weighted-count rule can subsequently flatten it.
"""
from sqlglot import exp
import re

_INTEGER = ("INT","INTEGER","INT64","BIGINT","SMALLINT","TINYINT")
COLLATION_ASSUMPTION = "string equality in singleton/group joins uses the same collation as right-side GROUP BY (no implicit collation coercion)"

def _same_comparison_type(left,right):
    # A comparison may otherwise match several distinct right-side groups:
    # BIGINT 1 matches VARCHAR '1' and '01' after numeric coercion, for example.
    if left in _INTEGER and right in _INTEGER:return True
    return left==right and bool(re.fullmatch(r"STRING|TEXT|VARCHAR(?:\(\d+\))?",left))

_EXTRAS = ("distinct", "order", "limit", "offset", "qualify", "windows", "with", "with_", "laterals", "having")

def _plain(select, group=False):
    return (isinstance(select, exp.Select) and not any(select.args.get(k) for k in _EXTRAS)
            and (group or not select.args.get("group"))
            and not any(select.find_all(exp.Window, exp.Exists)))

def _and(node):
    if isinstance(node, exp.Paren):
        return _and(node.this)
    return _and(node.this)+_and(node.expression) if isinstance(node,exp.And) else [node]

def _table(select):
    source=select.args.get("from_") or select.args.get("from")
    table=source.this if source else None
    if not isinstance(table,exp.Table) or table.args.get("db") or table.args.get("catalog") or table.args.get("joins"):
        return None
    alias=table.args.get("alias")
    return None if alias and alias.args.get("columns") else table

def singleton_count_sum(select, keys, types, assumptions=None):
    if not keys or not types or not _plain(select) or select.args.get("where"):
        return None
    joins=select.args.get("joins") or []
    source=select.args.get("from_") or select.args.get("from")
    if not source or len(joins)!=1:
        return None
    join=joins[0];left=source.this;right=join.this
    if join.args.get("side") or join.args.get("kind") not in (None,"","INNER") or any(join.args.get(k) for k in ("using","method","global_")):
        return None
    if not all(isinstance(s,exp.Subquery) and s.alias and isinstance(s.this,exp.Select)
               and not (s.args.get("alias").args.get("columns")) for s in (left,right)):
        return None
    a,b=left.this,right.this
    if not _plain(a) or a.args.get("joins") or not _plain(b,group=True) or b.args.get("joins") or b.args.get("where"):
        return None
    table,other=_table(a),_table(b)
    if table is None or other is None or not a.args.get("where"):
        return None
    # Do not reason through correlated/scalar input expressions.
    if list(a.find_all(exp.Subquery,exp.AggFunc)) or list(b.find_all(exp.Subquery)):
        return None
    passed={}
    for item in a.expressions:
        value=item.unalias()
        if not isinstance(value,exp.Column) or value.name=="*" or value.table.lower() not in ("",table.alias_or_name.lower()):
            return None
        name=item.alias_or_name.lower()
        if not name or name in passed:return None
        passed[name]=value.name.lower()
    fixed=set()
    for predicate in _and(a.args["where"].this):
        if not isinstance(predicate,exp.EQ):continue
        for column,literal in ((predicate.this,predicate.expression),(predicate.expression,predicate.this)):
            if isinstance(column,exp.Column) and column.table.lower() in ("",table.alias_or_name.lower()) and isinstance(literal,exp.Literal) and not literal.is_string:
                kind=types.get(table.name.lower(),{}).get(column.name.lower(),"").upper()
                # In particular, a MySQL STRING key compared to a number can
                # coerce many distinct keys to the same numeric value.
                if kind in _INTEGER and re.fullmatch(r"\d+",literal.this) and int(literal.this)<=2**31-1:
                    fixed.add(column.name.lower())
    declared={k.lower():v for k,v in keys.items()}.get(table.name.lower(),[])
    if not any(key and {c.lower() for c in key}<=fixed for key in declared):
        return None
    group=b.args.get("group")
    if not group or any(group.args.get(k) for k in ("grouping_sets","rollup","cube","totals")):
        return None
    group_keys={}
    for item in group.expressions:
        if not isinstance(item,exp.Column) or item.table.lower() not in ("",other.alias_or_name.lower()):return None
        group_keys[item.name.lower()]=None
    count_name=None;named_keys=set();key_columns={}
    for item in b.expressions:
        value=item.unalias();name=item.alias_or_name.lower()
        if isinstance(value,exp.Column) and value.name.lower() in group_keys and value.table.lower() in ("",other.alias_or_name.lower()):
            if name in named_keys:return None
            named_keys.add(name);group_keys[value.name.lower()]=name;key_columns[name]=value.name.lower()
        elif isinstance(value,exp.Count) and isinstance(value.this,exp.Star) and not value.args.get("distinct") and count_name is None:
            count_name=name
        else:return None
    if not count_name or count_name in named_keys or any(v is None for v in group_keys.values()):return None
    conditions=_and(join.args.get("on"))
    matched=set();string_keys=False
    for predicate in conditions:
        if not isinstance(predicate,exp.EQ):return None
        for x,y in ((predicate.this,predicate.expression),(predicate.expression,predicate.this)):
            if isinstance(x,exp.Column) and isinstance(y,exp.Column) and x.table.lower()==right.alias.lower() and x.name.lower() in named_keys and y.table.lower()==left.alias.lower() and y.name.lower() in passed:
                lkind=types.get(table.name.lower(),{}).get(passed[y.name.lower()],"").upper()
                rkind=types.get(other.name.lower(),{}).get(key_columns[x.name.lower()],"").upper()
                if not _same_comparison_type(lkind,rkind):return None
                string_keys |= lkind not in _INTEGER
                matched.add(x.name.lower());break
        else:return None
    if matched!=named_keys:return None
    products=[];grouping=[]
    for index,item in enumerate(select.expressions):
        value=item.unalias()
        if isinstance(value,exp.Column) and value.table.lower()==left.alias.lower() and value.name.lower() in passed:
            grouping.append(value.copy());continue
        if not isinstance(value,exp.Mul):return None
        for weight,count in ((value.this,value.expression),(value.expression,value.this)):
            if (isinstance(weight,exp.Column) and weight.table.lower()==left.alias.lower() and weight.name.lower() in passed
                    and isinstance(count,exp.Column) and count.table.lower()==right.alias.lower() and count.name.lower()==count_name):
                kind=types.get(table.name.lower(),{}).get(passed[weight.name.lower()],"").upper()
                if kind not in _INTEGER:return None
                products.append(index);break
        else:return None
    if not products or not grouping:return None
    if string_keys:
        # Normalizers without an assumption channel cannot consume this
        # conditional identity. The public algebraic proof collects it.
        if assumptions is None:return None
        assumptions.add(COLLATION_ASSUMPTION)
    result=select.copy()
    for index in products:
        item=result.expressions[index];value=item.unalias()
        replacement=exp.Sum(this=value.copy())
        if isinstance(item,exp.Alias):item.set("this",replacement)
        else:item.replace(replacement)
    result.set("group",exp.Group(expressions=grouping))
    return result
