"""Explicit Anthropic-compatible image/text transport for document parsing."""
import re


def request(messages, model, max_tokens):
    system=[]; converted=[]
    for message in messages:
        role=message.get('role'); content=message.get('content')
        if role=='system':
            if not isinstance(content,str):raise ValueError('系统提示类型无效')
            system.append(content);continue
        if role not in {'user','assistant'}:raise ValueError('视觉协议不接受此消息角色')
        if isinstance(content,str):blocks=[{'type':'text','text':content}]
        elif isinstance(content,list):
            blocks=[]
            for item in content:
                if item.get('type')=='text' and isinstance(item.get('text'),str):blocks.append({'type':'text','text':item['text']})
                elif item.get('type')=='image_url':
                    url=item.get('image_url',{}).get('url','')
                    match=re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)',url)
                    if not match:raise ValueError('视觉协议仅发送已预览的内联图片')
                    blocks.append({'type':'image','source':{'type':'base64','media_type':match[1],'data':match[2]}})
                else:raise ValueError('视觉协议图片或文字块无效')
        else:raise ValueError('视觉协议消息内容无效')
        converted.append({'role':role,'content':blocks})
    result={'model':model,'max_tokens':max_tokens,'stream':False,'messages':converted}
    if model.startswith('qwen3.8-'):result['thinking']={'type':'disabled'}
    if system:result['system']='\n'.join(system)
    return result


def response(raw, model):
    if raw.get('stop_reason')=='max_tokens':raise ValueError('视觉模型输出被截断，请缩小解析页范围')
    content=raw.get('content')
    if not isinstance(content,list):raise ValueError('视觉模型未返回文字块')
    text=''.join(block.get('text','') for block in content if isinstance(block,dict) and block.get('type')=='text' and isinstance(block.get('text'),str))
    if not text.strip() or len(text)>30000:raise ValueError('视觉模型回答为空或超限')
    usage=raw.get('usage') or {}; counts={}
    for source,target in [('input_tokens','prompt_tokens'),('output_tokens','completion_tokens')]:
        if type(usage.get(source)) is int and usage[source]>=0:counts[target]=usage[source]
    if len(counts)==2:counts['total_tokens']=sum(counts.values())
    return {'text':text,'model':str(raw.get('model',model))[:150],'finish_reason':str(raw.get('stop_reason',''))[:40],'usage':counts}
