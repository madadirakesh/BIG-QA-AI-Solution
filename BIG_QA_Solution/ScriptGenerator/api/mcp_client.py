import asyncio
import os
import sys
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# We'll use the existing AI clients from backend if possible, or fallback to direct API call
from api.backend import _get_openai_client, AI_MODEL, AI_TOOL, _get_gemini_client, API_KEY

async def run_mcp_git_prompt(prompt: str, project_path: str):
    """
    Connects to the Git MCP Server, retrieves tools, sends them to the LLM,
    and executes the tool call the LLM decides on.
    """
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server_git"],
        env={**os.environ, "GIT_DIR": os.path.join(project_path, ".git"), "GIT_WORK_TREE": project_path}
    )

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                # 1. Get tools from MCP Server
                tools_response = await session.list_tools()
                
                # Define simple fallback if mcp standard changes
                available_tools = []
                for t in tools_response.tools:
                    available_tools.append({
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.inputSchema
                    })

                # Format system message
                system_message = f"You are an intelligent Git Assistant. You have access to Git tools via MCP for the repository at {project_path}. Execute the user's request. If a tool call is needed, return ONLY a raw JSON string like: {{\"tool\": \"tool_name\", \"arguments\": {{\"key\": \"value\"}}}}."
                
                # 2. Call LLM (using simple OpenAI style for now, as standard tool-calling is complex across providers)
                # For simplicity in this integration, we ask the LLM to output a JSON tool call block manually.
                # In a robust production app, we would map `available_tools` to the native provider's tool schema.
                
                tools_context = json.dumps(available_tools, indent=2)
                full_prompt = f"{system_message}\n\nAvailable Tools:\n{tools_context}\n\nUser Request: {prompt}"
                
                # Use Gemini or OpenAI based on config
                llm_response_text = ""
                if AI_TOOL in ["GEMINI", "GOOGLE"]:
                    client = _get_gemini_client()
                    resp = client.models.generate_content(
                        model=AI_MODEL,
                        contents=full_prompt
                    )
                    llm_response_text = resp.text
                else:
                    client = _get_openai_client()
                    resp = client.chat.completions.create(
                        model=AI_MODEL,
                        messages=[{"role": "user", "content": full_prompt}],
                        temperature=0
                    )
                    llm_response_text = resp.choices[0].message.content

                # 3. Parse LLM response to see if it requested a tool
                try:
                    # Look for JSON block
                    start = llm_response_text.find('{')
                    end = llm_response_text.rfind('}')
                    if start != -1 and end != -1:
                        tool_call = json.loads(llm_response_text[start:end+1])
                        if "tool" in tool_call and "arguments" in tool_call:
                            tool_name = tool_call["tool"]
                            args = tool_call["arguments"]
                            
                            # 4. Execute the tool on the MCP server
                            result = await session.call_tool(tool_name, args)
                            
                            # Extract result text
                            output_text = "\n".join([c.text for c in result.content if hasattr(c, 'text')])
                            
                            # 5. Optional: Summarize result (skipping for speed, just return output)
                            return {"success": True, "message": f"Executed '{tool_name}'", "output": output_text}
                except Exception as e:
                    pass # Fallthrough to return the raw text if it wasn't a tool call

                # If no tool call was parsed, return the LLM's conversational response
                return {"success": True, "message": "AI Responded", "output": llm_response_text}

    except Exception as e:
        return {"success": False, "message": f"MCP Error: {str(e)}"}
