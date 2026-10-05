# Smart AI Project: SAP HANA Intelligence Agent

This project is an autonomous, AI-powered search engine and data agent built specifically for SAP Business One. It translates natural language questions into optimized, read-only SAP HANA SQL queries, executing them in real-time to provide immediate business intelligence. 

By leveraging advanced NLP and a single-pass reasoning loop, the engine autonomously discovers custom database schemas, enforces strict security guardrails, and formats the live data into interactive analytics dashboards.

## ✨ Key Features
* **Natural Language to SQL:** Query complex ERP data (sales, inventory, purchasing) using everyday language instead of writing database queries.
* **Autonomous Schema Discovery:** Automatically searches SAP B1 metadata (`CUFD`) to locate custom dimensions (e.g., custom Branch or Brand fields) without manual mapping.
* **Speed-Optimized Engine:** Utilizes prompt caching, in-memory schema caching, and a single-pass loop architecture to deliver results in roughly 3 seconds.
* **Enterprise-Grade Security:** A deterministic read-only guardrail strictly blocks all DML operations (INSERT, UPDATE, DELETE) and complex CTEs to protect the live database.
* **Auto-Correction:** Features self-healing capabilities to recover from minor JSON formatting errors without crashing.

## 🛠️ Tech Stack
* **Backend:** Python (FastAPI)
* **Database:** SAP HANA (via VZone Gateway)
* **AI/LLM:** Anthropic Claude (Sonnet)
* **Data Validation:** Pydantic

## 🚀 Setup and Installation

**1. Clone the repository and navigate to the project directory:**
```bash
git clone [https://github.com/yourusername/your-repo-name.git](https://github.com/yourusername/your-repo-name.git)
cd your-repo-name

2. Configure your environment variables:
Create a .env file in the root directory and add your necessary API keys and database credentials:

Code snippet
ANTHROPIC_API_KEY=your_api_key_here
CLAUDE_MODEL=gemini-3-5-flash
# Add any SAP HANA / VZone connection URLs here
3. Install the required dependencies:
It is recommended to use a virtual environment. Install the necessary Python packages using pip:

Bash
pip install -r requirements.txt
4. Run the server:
Start the FastAPI backend using Uvicorn with live reloading enabled:

Bash
uvicorn backend.main:app --reload
The server will start running locally at http://127.0.0.1:8000

📊 Usage Examples
Once the server is running, the AI can seamlessly handle analytical search queries such as:

"Show sales value by branch and product category so that I can compare performance."

"Give me the top 20 suppliers by purchase value."
