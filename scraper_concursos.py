import os
import json
import re
import urllib.parse
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from google import genai
from pydantic import BaseModel, Field

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")

ARQUIVO_HISTORICO = "historico_vagas.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

class AnaliseEdital(BaseModel):
    inscricoes_abertas: bool = Field(
        description="True APENAS se o edital está com inscrições abertas hoje (ou abre nos próximos dias). False se for previsto, suspenso, gabarito ou encerrado."
    )
    esfera_valida: bool = Field(
        description="True APENAS se for Prefeitura Municipal ou Secretaria Estadual (SEDUC/SED). False se for IF, UF, Universidade ou Militar."
    )
    jornada_horas: int = Field(
        description="Carga horária semanal para Professor de História (ex: 20, 30, 40). Se não informar, coloque 0."
    )
    salario_bruto: float = Field(
        description="Salário bruto em R$ mencionado para Professor (ou o maior valor do edital se não especificar por cargo)."
    )
    aprovado_regra_jornada_salario: bool = Field(
        description="True se jornada_horas == 40, OU se salario_bruto >= 3000.0, OU se o resumo não especificar horas mas o salário do edital passar de 3000."
    )
    usa_pnd_como_principal: bool = Field(
        description="True SE E SOMENTE SE o concurso utilizar a Prova Nacional Docente (PND / CNU Professores / INEP) como fase principal ou nota classificatória."
    )
    orgao: str = Field(description="Nome da Prefeitura ou Secretaria Estadual e UF (ex: Prefeitura de Pelotas - RS)")
    fim_inscricoes: str = Field(description="Data limite das inscrições (ex: 28/10/2026) ou 'Consultar edital'")
    resumo_vaga: str = Field(description="Resumo curto com vagas, salário, carga horária e banca.")

def carregar_historico():
    if os.path.exists(ARQUIVO_HISTORICO):
        try:
            with open(ARQUIVO_HISTORICO, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def salvar_historico(historico):
    with open(ARQUIVO_HISTORICO, "w", encoding="utf-8") as f:
        json.dump(list(historico), f, ensure_ascii=False, indent=2)

def enviar_telegram(mensagem):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("AVISO: TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID vazios nos Secrets!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        print(f"Status envio Telegram: {resp.status_code} - {resp.text[:120]}")
    except Exception as e:
        print(f"Erro ao conectar no Telegram: {e}")

def extrair_vagas():
    vagas = []
    urls_vistas = set()

    # FONTE 1: PCI Concursos
    try:
        url_pci = "https://www.pciconcursos.com.br/cargos/professor-de-historia"
        resp = requests.get(url_pci, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "/noticias/" in href and href not in urls_vistas:
                titulo = a_tag.get_text(strip=True)
                if len(titulo) > 10:
                    urls_vistas.add(href)
                    vagas.append({"titulo": titulo, "url": href, "resumo_extra": ""})
    except Exception as e:
        print(f"Aviso Fonte 1 (PCI): {e}")

    # FONTE 2: Google News RSS
    try:
        query = urllib.parse.quote('("Professor de História" OR "Docente - História") (edital OR concurso OR inscrições OR PND) (prefeitura OR secretaria)')
        rss_url = f"https://news.google.com/rss/search?q={query}+when:7d&hl=pt-BR&gl=BR&ceid=BR:pt-419"
        resp_rss = requests.get(rss_url, headers=HEADERS, timeout=20)
        soup_rss = BeautifulSoup(resp_rss.text, "xml")

        for item in soup_rss.find_all("item")[:15]:
            titulo = item.title.text if item.title else ""
            link = item.link.text if item.link else ""
            descricao = item.description.text if item.description else ""
            if link and link not in urls_vistas:
                urls_vistas.add(link)
                vagas.append({"titulo": titulo, "url": link, "resumo_extra": descricao})
    except Exception as e:
        print(f"Aviso Fonte 2 (RSS): {e}")

    regex_instituicoes_excluidas = r"\b(IF[A-Z]+|UF[A-Z]+|UTFPR|CEFET|UNIVERSIDADE|FACULDADE)\b"
    regex_nao_abertos = r"\b(GABARITO|RESULTADO|CONVOCA[ÇC][ÃA]O|HOMOLOGA[ÇC][ÃA]O|SUSPENSO|CANCELADO|ADIADO|PREVISTO|COMISS[ÃA]O FORMADA|AUTORIZADO)\b"

    vagas_filtradas = []
    for v in vagas:
        titulo_upper = v["titulo"].upper()
        if re.search(regex_instituicoes_excluidas, titulo_upper):
            continue
        if re.search(regex_nao_abertos, titulo_upper):
            continue
        vagas_filtradas.append(v)

    print(f"Total de vagas abertas pre-selecionadas hoje: {len(vagas_filtradas)}")
    return vagas_filtradas[:12]

def analisar_vaga_com_ia(client, vaga, data_hoje):
    texto_limpo = vaga["resumo_extra"]
    try:
        resp = requests.get(vaga["url"], headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            conteudo = soup.select_one("div#concursos") or soup.select_one("article") or soup.body
            if conteudo:
                texto_limpo += "\n" + conteudo.get_text(separator="\n", strip=True)[:12000]
    except Exception as e:
        print(f"Usando apenas resumo para {vaga['titulo']}: {e}")

    prompt = f"""
    Data de hoje: {data_hoje}.
    Analise esta notícia/edital de concurso público para PROFESSOR DE HISTÓRIA.
    Título: {vaga['titulo']}
    Texto:
    {texto_limpo}

    Regras estritas de aprovação:
    1. INSCRIÇÕES ABERTAS: Verifique se o edital está com inscrições abertas (ou prestes a abrir) considerando a data de hoje ({data_hoje}). Se o prazo final já passou, ou se for apenas previsto/comissão sem edital, marque inscricoes_abertas = False.
    2. Esfera: DEVE ser Prefeitura Municipal ou Secretaria Estadual. Rejeite IFs, UFs e Universidades.
    3. Jornada e Salário: Aprove se for 40h semanais (qualquer salário) OU se for 20h/30h com salário > R$ 3.000,00.
    4. PND: Marque usa_pnd_como_principal = True se o concurso usar a Prova Nacional Docente (PND / INEP) como nota principal.
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={
            "response_mime_type": "application/json",
            "response_schema": AnaliseEdital,
            "temperature": 0.1
        }
    )
    return AnaliseEdital.model_validate_json(response.text)

def main():
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    print(f"Iniciando execucao do Radar em {data_hoje}...")

    historico = carregar_historico()
    salvar_historico(historico) # Garante que o arquivo sempre exista

    vagas = extrair_vagas()

    # Envia o sinal de vida no Telegram ANTES da IA começar!
    enviar_telegram(
        f"🤖 <b>Radar de Concursos Ativo! ({data_hoje})</b>\n"
        f"🔎 Encontrei <b>{len(vagas)}</b> notícias recentes de Professor de História. Filtrando editais abertos agora..."
    )

    if not GEMINI_API_KEY:
        print("ERRO: GEMINI_API_KEY nao configurada nos Secrets!")
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    aprovadas_hoje = 0

    for v in vagas:
        if v["url"] in historico:
            continue

        try:
            print(f"Analisando com IA: {v['titulo']}...")
            resultado = analisar_vaga_com_ia(client, v, data_hoje)
            historico.add(v["url"])

            if resultado.inscricoes_abertas and resultado.esfera_valida and resultado.aprovado_regra_jornada_salario:
                aprovadas_hoje += 1
                if resultado.usa_pnd_como_principal:
                    cabecalho = "🚨🔥 <b>ALERTA MÁXIMO: EDITAL ABERTO COM PND PRINCIPAL!</b> 🔥🚨"
                else:
                    cabecalho = "✅ <b>Concurso Aberto Aprovado (40h / >R$3k)</b>"

                jornada_str = f"{resultado.jornada_horas}h" if resultado.jornada_horas > 0 else "Ver edital"
                msg = (
                    f"{cabecalho}\n\n"
                    f"🏛 <b>Órgão:</b> {resultado.orgao}\n"
                    f"📅 <b>Inscrições até:</b> {resultado.fim_inscricoes}\n"
                    f"⏱ <b>Jornada:</b> {jornada_str} | 💰 <b>Salário:</b> R$ {resultado.salario_bruto:.2f}\n"
                    f"📋 <b>Detalhes:</b> {resultado.resumo_vaga}\n\n"
                    f"🔗 <a href='{v['url']}'>Acessar notícia / Edital</a>"
                )
                enviar_telegram(msg)
        except Exception as e:
            print(f"Erro ao processar {v['titulo']}: {e}")

    salvar_historico(historico)

    if aprovadas_hoje == 0:
        enviar_telegram("🏁 Varredura concluída! Nenhuma das vagas novas de hoje passou no filtro estrito (Inscrições Abertas + Estadual/Municipal + 40h ou >R$ 3k).")

if __name__ == "__main__":
    main()
