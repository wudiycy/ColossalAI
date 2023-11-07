import os
import json 
import requests 
import http.client
from langchain.llms.base import LLM
from langchain.utils import get_from_dict_or_env
from typing import Any, List, Mapping, Optional


class Pangu(LLM):
    """
    A custom LLM class that integrates pangu models
    
    """
    id: int
    gen_config: dict = None  
    auth_config: dict = None

    def __init__(self, gen_config=None, **kwargs):
        super(Pangu, self).__init__(**kwargs)
        self.id = id
        if gen_config is None: 
            self.gen_config = {
                                "user": "User",
                                "max_tokens": 50,
                                "temperature": 0.95,
                                "n": 1
                            } 
        else: 
            self.gen_config = gen_config
            
    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {"id": self.id}

    @property
    def _llm_type(self) -> str:
        return 'pangu'
    
    def _call(self, prompt: str, stop: Optional[List[str]]=None, **kwargs) -> str:
        """
        Args:
            prompt: The prompt to pass into the model.
            stop: A list of strings to stop generation when encountered

        Returns:
            The string generated by the model        
        """
        # Update the generation arguments
        for key, value in kwargs.items():
            if key in self.gen_config:
                self.gen_config[key] = value
    
        response = self.text_completion(prompt, self.gen_config, self.auth_config)
        text = response['choices'][0]['text']
        if stop is not None:
            for stopping_words in stop:
                if stopping_words in text:
                    text = text.split(stopping_words)[0]
        return text
    
    def set_auth_config(self, **kwargs):
        url = get_from_dict_or_env(kwargs, "url", "URL")
        username = get_from_dict_or_env(kwargs, "username", "USERNAME")
        password = get_from_dict_or_env(kwargs, "password", "PASSWORD")
        domain_name = get_from_dict_or_env(kwargs, "domain_name", "DOMAIN_NAME")

        region = url.split('.')[1]
        auth_config = {}
        auth_config['endpoint'] = url[url.find("https://")+8:url.find(".com")+4]
        auth_config['resource_path'] = url[url.find(".com")+4:]
        auth_config['auth_token'] = self.get_latest_auth_token(region, username, password, domain_name)
        self.auth_config = auth_config

    def get_latest_auth_token(self, region, username, password, domain_name):
        url = f"https://iam.{region}.myhuaweicloud.com/v3/auth/tokens" 
        payload = json.dumps({ 
          "auth": { 
            "identity": { 
              "methods": [ 
                "password" 
              ], 
              "password": { 
                "user": { 
                  "name": username, 
                  "password": password, 
                  "domain": { 
                    "name": domain_name
                  } 
                } 
              } 
            }, 
            "scope": { 
              "project": { 
                "name": region 
              } 
            } 
          } 
        }) 
        headers = { 
          'Content-Type': 'application/json' 
        } 

        response = requests.request("POST", url, headers=headers, data=payload) 
        return response.headers["X-Subject-Token"]

    def text_completion(self, text, gen_config, auth_config):
        conn = http.client.HTTPSConnection(auth_config['endpoint'])
        payload = json.dumps({
          "prompt": text,
          "user": gen_config['user'],
          "max_tokens": gen_config["max_tokens"],
          "temperature": gen_config['temperature'],
          "n": gen_config['n']
        })
        headers = {
          'X-Auth-Token': auth_config['auth_token'],
          'Content-Type': 'application/json',
        }
        conn.request("POST", auth_config['resource_path'], payload, headers)
        res = conn.getresponse()
        data = res.read()
        data = json.loads(data.decode("utf-8"))
        return data

    def chat_model(self, messages, gen_config, auth_config):
        conn = http.client.HTTPSConnection(auth_config['endpoint'])
        payload = json.dumps({
          "messages": messages,
          "user": gen_config['user'],
          "max_tokens": gen_config["max_tokens"],
          "temperature": gen_config['temperature'],
          "n": gen_config['n']
        })
        headers = {
          'X-Auth-Token': auth_config['auth_token'],
          'Content-Type': 'application/json',
        }
        conn.request("POST", auth_config['resource_path'], payload, headers)
        res = conn.getresponse()
        data = res.read()
        data = json.loads(data.decode("utf-8"))
        return data
    
if __name__ == '__main__':
    # URL: “盘古大模型套件管理”->点击“服务管理”->“模型列表”->点击想要使用的模型的“复制路径”
    # USERNAME: 华为云控制台：“我的凭证”->“API凭证”下的“IAM用户名”，也就是你登录IAM账户的名字
    # PASSWORD: IAM用户的密码
    # DOMAIN_NAME: 华为云控制台：“我的凭证”->“API凭证”下的“用户名”，也就是公司管理IAM账户的总账户名
    os.environ["URL"] = ""
    os.environ["URLNAME"] = ""
    os.environ["PASSWORD"] = ""
    os.environ["DOMAIN_NAME"] = ""

    pg = Pangu(id=1)
    pg.set_auth_config()

    print(pg('你是谁'))  # 您好,我是华为盘古大模型。我能够通过和您对话互动为您提供帮助。请问您有什么想问我的吗?