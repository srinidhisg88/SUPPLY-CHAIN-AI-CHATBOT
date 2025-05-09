from fastapi import FastAPI, HTTPException, Query
from langchain_community.agent_toolkits.sql.base import create_sql_agent
from langchain_community.utilities import SQLDatabase
from langchain.agents.agent_types import AgentType
from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
from sqlalchemy import create_engine, text
from langchain_groq import ChatGroq
import json
import re
from dotenv import load_dotenv
load_dotenv()
import os

api_key  = os.getenv("GROQ_API_KEY")

app = FastAPI()

@app.get("/")
def read_root():    
    return{"hello":"world"}


def configure_db(host: str, user: str, password: str, database: str):
    try:
        conn_string = f"postgresql+psycopg2://{user}:{password}@{host}/{database}"
        engine = create_engine(conn_string)
        return SQLDatabase(engine), engine
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")

def extract_sql_query(agent_response):
    if re.match(r'^[\d.]+$', agent_response.strip()):
        raise ValueError(f"Agent returned a numeric value instead of SQL: {agent_response}")

    sql_match = re.search(r'```sql\s*(.*?)\s*```', agent_response, re.DOTALL)
    if sql_match:
        return sql_match.group(1).strip()

    sql_match = re.search(r'`(.*?)`', agent_response, re.DOTALL)
    if sql_match:
        return sql_match.group(1).strip()

    sql_keywords = ['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER', 'DROP', 'SHOW']
    for keyword in sql_keywords:
        sql_match = re.search(f'{keyword}\\s+.*', agent_response, re.IGNORECASE | re.DOTALL)
        if sql_match:
            return sql_match.group(0).strip()

    if any(keyword in agent_response.upper() for keyword in sql_keywords):
        return agent_response.strip()

    raise ValueError(f"Could not identify SQL query in agent response: {agent_response}")

def is_valid_sql(query):
    if re.match(r'^[\d.]+$', query.strip()):
        return False

    sql_keywords = ['SELECT', 'FROM', 'WHERE', 'INSERT', 'UPDATE', 'DELETE', 'JOIN', 'GROUP BY', 'ORDER BY']
    return any(keyword.upper() in query.upper() for keyword in sql_keywords)

@app.post("/chat")
async def chat_with_db(
    host: str = Query(..., description="PostgreSQL host"),
    user: str = Query(..., description="PostgreSQL user"),
    password: str = Query(..., description="PostgreSQL password"),
    database: str = Query(..., description="PostgreSQL database"),
    query: str = Query(..., description="User query to generate SQL")
):
    try:
        llm = ChatGroq(
            groq_api_key=api_key,
            model_name="llama-3.1-8b-instant",
            streaming=False
        )

        db, engine = configure_db(host, user, password, database)

        toolkit = SQLDatabaseToolkit(db=db, llm=llm)
        agent = create_sql_agent(
            llm=llm,
            toolkit=toolkit,
            verbose=True,
            agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION
        )

        sql_generation_prompt = f"""
        For the following question, generate a valid SQL query to answer it.
        Question: "{query}"

        You must return a valid SQL query that would run in PostgreSQL.
        Start with the SQL keywords like SELECT, INSERT, UPDATE, etc.
        DO NOT include explanations, markdown formatting, or anything else - ONLY the SQL query itself.
        """

        agent_response = agent.run(sql_generation_prompt)

        try:
            sql_query = extract_sql_query(agent_response)
            if not is_valid_sql(sql_query):
                raise ValueError(f"The generated query doesn't appear to be valid SQL: {sql_query}")
        except ValueError as e:
            retry_prompt = f"""
            The previous response didn't contain a valid SQL query. 
            Please generate a valid SQL query (starting with SELECT, INSERT, etc.) for this question: 
            "{query}"
            Return ONLY the SQL query itself, no explanations.
            """
            agent_response = agent.run(retry_prompt)
            sql_query = extract_sql_query(agent_response)
            if not is_valid_sql(sql_query):
                raise ValueError(f"Failed to generate valid SQL after retry: {sql_query}")

        sql_result_list = []
        with engine.connect() as connection:
            result = connection.execute(text(sql_query))
            if result.returns_rows:
                columns = result.keys()
                for row in result:
                    row_dict = {col: value for col, value in zip(columns, row)}
                    for key, value in row_dict.items():
                        if not isinstance(value, (str, int, float, bool, type(None))):
                            row_dict[key] = str(value)
                    sql_result_list.append(row_dict)
                sql_result_str = json.dumps(sql_result_list)
            else:
                sql_result_str = "Query executed successfully. No rows returned."

        summary_prompt = f"""
        Question: {query}
        SQL Query: {sql_query}
        SQL Result: {sql_result_str}

        Please provide a clear, concise summary of these results in natural language.
        """

        summary = llm.invoke(summary_prompt).content

        return {
            "user_query": query,
            "sql_query": sql_query,
            "sql_result": sql_result_list if result.returns_rows else "Query executed successfully. No rows returned.",
            "summary": summary
        }

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        raise HTTPException(status_code=500, detail=f"Error processing query: {str(e)}\n{error_details}")
    

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))  # Render will inject PORT
    uvicorn.run("app:app", host="0.0.0.0", port=port)