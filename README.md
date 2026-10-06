# Arquitetura

`aurora/api.py` expõe a API FastAPI do contrato e executa o agente por um `Runner` do Google ADK. O `root_agent`, em `aurora/agents.py`, recebe as mensagens e transfere cada assunto ao especialista correspondente. A árvore usa três especialistas para manter a busca do regulamento separada das operações que alteram o condomínio:

- `especialista_reservas` consulta, cria e cancela reservas usando tools.
- `especialista_visitantes` consulta e solicita autorizações usando tools.
- `especialista_regulamento` chama uma tool de busca pontual e responde somente com os trechos retornados.

Os especialistas são subagents do agente principal; o principal não recebe o conteúdo do regulamento nem tools de gravação. As sessões do ADK e o estado do condomínio são persistidos em `.aurora/aurora.sqlite3`. Não há serviço externo para iniciar.

# Garantias

1. **Cobrança e acesso exigem confirmação do sistema.** Em `aurora/tools.py`, `reserve_common_area` pede confirmação se `fee > 0`, e `authorize_visitor` sempre pede confirmação. O texto da conversa não é consultado para aprovar essas ações. Em `aurora/api.py`, `POST /sessoes/{session_id}/confirmacoes` exige um ID pendente daquela sessão, envia a resposta ADK como `FunctionResponse` e retoma o `invocation_id` original. `claim_confirmation` em `aurora/storage.py` muda o estado para `processing` em uma transação SQLite; IDs inexistentes ou já consumidos recebem `409`.

   ```python
   if fee > 0 and confirmation is None:
       tool_context.request_confirmation(...)
   ```

2. **A sessão fica vinculada ao apartamento.** `create_session` em `aurora/api.py` valida o número e grava `apartment` no estado ADK e em `api_sessions`. `_apartment` em `aurora/tools.py` lê somente `ToolContext.state`; as tools não aceitam apartamento nos argumentos do modelo. As consultas e cancelamentos filtram no banco por esse valor, e a disponibilidade retorna somente livre/ocupada.

   ```python
   apartment = tool_context.state.get("apartment")
   ```

3. **Reiniciar preserva conversas e alterações.** O lifespan de `aurora/api.py` usa `SqliteSessionService` apontando para `.aurora/aurora.sqlite3`; as tabelas de reservas, visitantes, sessões da API e confirmações também ficam nesse arquivo. A restauração é explícita, via `uv run python -m aurora.restore`.

   ```python
   SqliteSessionService(db_path=str(DATABASE_PATH))
   ```

4. **O regulamento é consultado sob demanda.** `search_regulation` em `aurora/tools.py` lê `dados/regulamento.md` durante cada chamada e devolve no máximo dois trechos curtos classificados pela pergunta. Só `especialista_regulamento` recebe essa tool; nem `root_agent` nem as instruções dos agentes contêm o texto do regulamento. Assim, eventos de assuntos diferentes não herdam capítulos inteiros.

   ```python
   text = path.read_text(encoding="utf-8")
   ```

5. **Uma área/data tem no máximo uma reserva ativa.** `initialize` em `aurora/storage.py` cria o índice único parcial `one_active_reservation_per_area_date`. `reserve_atomically` executa `BEGIN IMMEDIATE` e insere sob esse índice; se outra aprovação ganhar a disputa, a restrição converte o conflito em resultado `occupied`, sem erro de servidor. `_action_key` em `aurora/tools.py` inclui o ID da sessão para não cruzar chaves de idempotência entre moradores. `reservation_codes` mantém códigos usados inclusive após cancelamentos e restaurações.

   ```sql
   CREATE UNIQUE INDEX IF NOT EXISTS one_active_reservation_per_area_date
       ON reservations(area, date) WHERE cancelled = 0;
   ```

# Como rodar

Pré-requisitos: Python 3.12 ou superior, `uv` e uma chave do Google AI Studio com acesso ao modelo Gemini configurado. O projeto fixa `google-adk==2.9.1` em `pyproject.toml`.

Na raiz do repositório:

```sh
uv sync
cp .env.example .env
```

Preencha `GOOGLE_API_KEY` no `.env`. `GOOGLE_GENAI_USE_VERTEXAI=FALSE` seleciona a API Gemini com chave do AI Studio. `GEMINI_MODEL` define o modelo Gemini; o padrão é `gemini-3.8-flash` e pode ser ajustado a um modelo habilitado no seu projeto.

Restaure os dados iniciais a qualquer momento:

```sh
uv run python -m aurora.restore
```

Inicie a API em `http://localhost:8000`:

```sh
uv run uvicorn aurora.api:app --host 0.0.0.0 --port 8000
```

As rotas de verificação leem diretamente o banco SQLite. O `.env` e o diretório `.aurora/` são locais e ignorados pelo Git; `.env.example` contém apenas os nomes das variáveis.
