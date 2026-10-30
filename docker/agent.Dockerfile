# Agent image for --isolation docker. Contains ONLY the agent side: the stdio adapter and the mock
# agents. No verifier code, policies, assertions, fixtures or evidence are in this image, and the
# runner mounts no volumes, so the gateway's stdio channel is the agent's only route to state.
FROM python:3.12-slim
WORKDIR /agent
COPY assure/__init__.py assure/adapter.py /agent/assure/
COPY assure/agents/__init__.py assure/agents/mock_agent.py /agent/assure/agents/
ENV PYTHONPATH=/agent PYTHONDONTWRITEBYTECODE=1
USER 65534:65534
ENTRYPOINT ["python", "-m", "assure.agents.mock_agent"]
