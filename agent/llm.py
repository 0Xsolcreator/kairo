from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="my-llm",
    base_url="http://localhost:11434/v1",
    api_key="no-key",
)
