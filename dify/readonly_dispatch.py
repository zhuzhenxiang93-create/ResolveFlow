"""Portable Dify tool dispatcher. Role selects read-only capabilities; it is not an authorization identity."""
import json, re
from decimal import Decimal, InvalidOperation
SCOPES = {
    "commerce": {"inspect_request_context", "knowledge_search"},
    "general": {"inspect_request_context", "knowledge_search", "create_handoff_summary"},
    "technical": {"inspect_request_context", "knowledge_search", "lookup_error_code"},
    "billing": {"knowledge_search", "check_billing_fields", "compare_amounts"},
    "escalation": {"inspect_request_context", "create_handoff_summary"},
}
ERRORS = {'401': '身份验证失败，通常是登录态过期或凭据错误，需要重新登录或重置密码。', '403': '权限不足，账号可能缺少对应权益或被限制访问该资源。', '404': '请求的资源不存在，常见于订单号、链接或页面拼写错误。', '429': '请求过于频繁被限流，建议提示用户稍后重试。', '500': '服务端内部错误，通常与本次输入无关，需要记录现场供技术团队排查。', '502': '网关或上游服务暂时不可用，多为临时性故障，建议重试。', '503': '服务暂时过载或正在维护，建议稍后重试。'}
def main(role: str, operation: str, query: str, arguments_json: str, context: str, documents: list):
    def finish(data, success=True):
        trace={"agent_type":role,"tool":operation,"arguments":args,"success":success,"result":data}
        return {"result":json.dumps({"success":success,"data":data,"trace":trace},ensure_ascii=False)}
    try: args=json.loads(arguments_json or "{}")
    except (TypeError,ValueError): args={};return finish({"error":"Arguments must be JSON"},False)
    if not isinstance(args,dict):args={};return finish({"error":"Arguments must be an object"},False)
    if operation not in SCOPES.get(role,set()):return finish({"error":"Tool not permitted for this role"},False)
    try: ctx=json.loads(context or "{}")
    except (TypeError,ValueError):return finish({"error":"Invalid context"},False)
    if operation=="lookup_error_code":
        c=str(args.get("code","")).strip()
        if not re.fullmatch(r"[45]\d\d",c):return finish({"error":"A three-digit error code is required"},False)
        return finish({"known":c in ERRORS,"code":c,"explanation":ERRORS.get(c,"未收录该错误码，需要进一步核查")})
    if operation=="inspect_request_context":return finish({"query":query,"known_context":ctx,"simulated":True})
    if operation=="knowledge_search":
        sources=[]
        for d in documents[:4]:
            meta=d.get("metadata",{})
            sources.append({"document_id":meta.get("document_id","")+":"+str(meta.get("segment_id",d.get("id",""))),"title":d.get("title",""),"source":"Dify knowledge retrieval","content":d.get("content","")})
        return finish({"query":query,"sources":sources,"found":bool(sources)})
    if operation=="check_billing_fields":
        text=ctx.get("message",query) if isinstance(ctx,dict) else query
        amounts=re.findall(r"(?<![A-Za-z0-9_-])\d+(?:\.\d{1,2})?\s*(?:元|块|人民币)",text)
        ids=re.findall(r"[GS]-[A-Za-z0-9_-]+",text)
        return finish({"known_order_ids":ids,"known_amounts":amounts,"missing_fields":(["order_id"] if not ids else [])+(["amount"] if not amounts else [])})
    if operation=="compare_amounts":
        try:
            a=Decimal(str(args["expected"]));b=Decimal(str(args["actual"]))
            if not a.is_finite() or not b.is_finite() or a<0 or b<0:raise ValueError()
            diff=(b-a).quantize(Decimal("0.01"))
        except (KeyError,InvalidOperation,ValueError,TypeError):return finish({"error":"Finite nonnegative expected and actual required"},False)
        return finish({"expected":str(a),"actual":str(b),"difference":str(diff),"overcharged":diff>0,"matches":diff==0,"basis":"user-provided amounts, not verified charges"})
    return finish({"summary":{"query":query,"known_context":ctx,"reason":str(args.get("reason","需要人工核查"))},"handoff_connected":False,"note":"仅生成交接摘要，没有接通真人或提交工单"})
