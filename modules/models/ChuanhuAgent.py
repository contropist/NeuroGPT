from langchain.chains.summarize import load_summarize_chain
from langchain import PromptTemplate, LLMChain
from langchain.chat_models import ChatOpenAI
from langchain.prompts import PromptTemplate
from langchain.text_splitter import TokenTextSplitter
from langchain.embeddings import OpenAIEmbeddings
from langchain.vectorstores import FAISS
from langchain.chains import RetrievalQA
from langchain.agents import load_tools
from langchain.agents import initialize_agent
from langchain.agents import AgentType
from langchain.docstore.document import Document
from langchain.tools import BaseTool, StructuredTool, Tool, tool
from langchain.callbacks.stdout import StdOutCallbackHandler
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from langchain.callbacks.manager import BaseCallbackManager
from duckduckgo_search import DDGS
from itertools import islice

from typing import Any, Dict, List, Optional, Union

from langchain.callbacks.base import BaseCallbackHandler
from langchain.input import print_text
from langchain.schema import AgentAction, AgentFinish, LLMResult

from langchain.llms.base import LLM
from langchain_g4f import G4FLLM

from pydantic import BaseModel, Field

import requests
from bs4 import BeautifulSoup
from threading import Thread, Condition
from collections import deque

from .base_model import BaseLLMModel, CallbackToIterator, ChuanhuCallbackHandler
from ..config import default_chuanhu_assistant_model
from ..presets import SUMMARIZE_PROMPT
from ..index_func import construct_index

from langchain.callbacks import get_openai_callback
import os
import gradio as gr
import logging

from g4f import Provider, models

class GoogleSearchInput(BaseModel):
    keywords: str = Field(description="keywords to search")

class WebBrowsingInput(BaseModel):
    url: str = Field(description="URL of a webpage")

class WebAskingInput(BaseModel):
    url: str = Field(description="URL of a webpage")
    question: str = Field(description="Question that you want to know the answer to, based on the webpage's content.")


class ChuanhuAgent_Client(BaseLLMModel):
    def __init__(self, model_name, openai_api_key, user_name="") -> None:
        super().__init__(model_name=model_name, user=user_name)
        self.text_splitter = TokenTextSplitter(chunk_size=500, chunk_overlap=30)
        self.api_key = openai_api_key
        self.llm: LLM = G4FLLM(temperature=0, model=models.gpt_35_turbo, provider=Provider.NeuroGPT)
        self.cheap_llm: LLM = G4FLLM(temperature=0, model=models.gpt_35_turbo, provider=Provider.NeuroGPT)
        PROMPT = PromptTemplate(template=SUMMARIZE_PROMPT, input_variables=["text"])
        self.summarize_chain = load_summarize_chain(self.cheap_llm, chain_type="map_reduce", return_intermediate_steps=True, map_prompt=PROMPT, combine_prompt=PROMPT)
        self.index_summary = None
        self.index = None
        if "Pro" in self.model_name:
            self.tools = load_tools(["serpapi", "google-search-results-json", "llm-math", "arxiv", "wikipedia", "wolfram-alpha"], llm=self.llm)
        else:
            self.tools = load_tools(["ddg-search", "llm-math", "arxiv", "wikipedia"], llm=self.llm)
            self.tools.append(
                Tool.from_function(
                    func=self.google_search_simple,
                    name="Google Search JSON",
                    description="useful when you need to search the web.",
                    args_schema=GoogleSearchInput
                )
            )

        self.tools.append(
            Tool.from_function(
                func=self.summary_url,
                name="Summary Webpage",
                description="useful when you need to know the overall content of a webpage.",
                args_schema=WebBrowsingInput
            )
        )

        self.tools.append(
            StructuredTool.from_function(
                func=self.ask_url,
                name="Ask Webpage",
                description="useful when you need to ask detailed questions about a webpage.",
                args_schema=WebAskingInput
            )
        )

    def google_search_simple(self, query):
        results = []
        with DDGS() as ddgs:
            ddgs_gen = ddgs.text("notes from a dead house", backend="api")
            for r in islice(ddgs_gen, 10):
                results.append({
                    "title": r["title"],
                    "link": r["href"],
                    "snippet": r["body"]
                })
        return str(results)

    def handle_file_upload(self, files, chatbot, language):
        """if the model accepts multi modal input, implement this function"""
        status = gr.Markdown.update()
        if files:
            index = construct_index(file_src=files)
            assert index is not None, "Сбой получения индексации"
            self.index = index
            status = "Создание индексации завершено"
            # Summarize the document
            logging.info("Генерирация краткого изложения контента……")
            with get_openai_callback() as cb:
                os.environ["OPENAI_API_KEY"] = self.api_key
                from langchain.chains.summarize import load_summarize_chain
                from langchain.prompts import PromptTemplate
                from langchain.chat_models import ChatOpenAI
                prompt_template = "Write a concise summary of the following:\n\n{text}\n\nCONCISE SUMMARY IN " + language + ":"
                PROMPT = PromptTemplate(template=prompt_template, input_variables=["text"])
                llm = G4FLLM(temperature=0, model=models.gpt_35_turbo, provider=Provider.NeuroGPT)
                chain = load_summarize_chain(llm, chain_type="map_reduce", return_intermediate_steps=True, map_prompt=PROMPT, combine_prompt=PROMPT)
                summary = chain({"input_documents": list(index.docstore.__dict__["_dict"].values())}, return_only_outputs=True)["output_text"]
                logging.info(f"Summary: {summary}")
                self.index_summary = summary
                chatbot.append((f"Uploaded {len(files)} files", summary))
            logging.info(cb)
        return gr.Files.update(), chatbot, status
    
    # ChuanhuAgent.py

    def handle_message(self, message):
        words = message.split()
    
        if words[0].lower() == '!search':
            keywords = ' '.join(words[1:])
            return self.google_search_simple({ 'keywords': keywords })
    
        elif words[0].lower() == '!summarize':
            url = words[1]
            return self.summary_url({ 'url': url })
    
        elif words[0].lower() == '!ask':
            url, question = words[1], ' '.join(words[2:])
            return self.ask_url({ 'url': url, 'question': question })
    
        return f'Unknown command: {words[0]}'

    def query_index(self, query):
        if self.index is not None:
            retriever = self.index.as_retriever()
            qa = RetrievalQA.from_chain_type(llm=self.llm, chain_type="stuff", retriever=retriever)
            return qa.run(query)
        else:
            "Error during query."

    def summary(self, text):
        texts = Document(page_content=text)
        texts = self.text_splitter.split_documents([texts])
        return self.summarize_chain({"input_documents": texts}, return_only_outputs=True)["output_text"]

    def fetch_url_content(self, url):
        response = requests.get(url)
        soup = BeautifulSoup(response.text, 'html.parser')

        # Извлеките весь текст
        text = ''.join(s.getText() for s in soup.find_all('p'))
        logging.info(f"Extracted text from {url}")
        return text

    def summary_url(self, url):
        text = self.fetch_url_content(url)
        if text == "":
            return "URL unavailable."
        text_summary = self.summary(text)
        url_content = "webpage content summary:\n" + text_summary

        return url_content

    def ask_url(self, url, question):
        text = self.fetch_url_content(url)
        if text == "":
            return "URL unavailable."
        texts = Document(page_content=text)
        texts = self.text_splitter.split_documents([texts])
        # use embedding
        embeddings = OpenAIEmbeddings(openai_api_key=self.api_key, openai_api_base=os.environ.get("OPENAI_API_BASE", None))

        # create vectorstore
        db = FAISS.from_documents(texts, embeddings)
        retriever = db.as_retriever()
        qa = RetrievalQA.from_chain_type(llm=self.cheap_llm, chain_type="stuff", retriever=retriever)
        return qa.run(f"{question} Reply in Русский")

    def get_answer_at_once(self):
        question = self.history[-1]["content"]
        # llm=ChatOpenAI(temperature=0, model_name="gpt-3.5-turbo")
        agent = initialize_agent(self.tools, self.llm, agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION, verbose=True)
        reply = agent.run(input=f"{question} Reply in Русский")
        return reply, -1

    def get_answer_stream_iter(self):
        question = self.history[-1]["content"]
        it = CallbackToIterator()
        manager = BaseCallbackManager(handlers=[ChuanhuCallbackHandler(it.callback)])
        def thread_func():
            tools = self.tools
            if self.index is not None:
                    tools.append(
                        Tool.from_function(
                        func=self.query_index,
                        name="Query Knowledge Base",
                        description=f"useful when you need to know about: {self.index_summary}",
                        args_schema=WebBrowsingInput
                    )
                )
            agent = initialize_agent(self.tools, self.llm, agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION, verbose=True, callback_manager=manager)
            try:
                reply = agent.run(input=f"{question} Reply in Русский")
            except Exception as e:
                import traceback
                traceback.print_exc()
                reply = str(e)
            it.callback(reply)
            it.finish()
        t = Thread(target=thread_func)
        t.start()
        partial_text = ""
        for value in it:
            partial_text += value
            yield partial_text
