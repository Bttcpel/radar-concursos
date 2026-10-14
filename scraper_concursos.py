import os
import sys
import time
import json
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from google import genai
from pydantic import BaseModel, Field

print("=== INICIANDO ROBO DE CONCURSOS V2 ===", flush=True)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")

# Mudamos o nome para zerar as 12 vagas que falharam no teste anterior!
ARQUIVO_HISTORICO = "historico_vagas_v2.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

class AnaliseEdital(BaseModel):
    inscricoes_abertas: bool = Field(description="True se o edital esta aberto ou abre em breve. False APENAS se o prazo ja encerrou claramente ou se for gabarito/suspenso.")
    esfera_valida: bool = Field(description="True se for Prefeitura Municipal, Camara ou Secretaria Estadual (SEDUC/SED). False se for IF, UF, Universidade ou Militar.")
    jornada_horas: int = Field(description="Carga horaria semanal para Professor de Historia (ex: 20, 30, 40). Se nao informar no resumo, coloque 0.")
    salario_bruto: float = Field(description="Maior salario bruto em R$ mencionado para Professor (ou o teto salarial do edital informado no resumo).")
    aprovado_regra_jornada_salario: bool = Field(description="True se jornada_horas == 40, OU se salario_bruto >= 3000.0, OU se jornada_horas == 0 e salario_bruto >= 3000.0.")
    usa_pnd_como_principal: bool = Field(description="True SE E SOMENTE SE mencionar Prova Nacional Docente (PND / INEP / CNU Professores) como forma de selecao.")
    orgao: str = Field(description="Nome da Prefeitura ou Secretaria Estadual e UF (ex: Prefeitura de Panambi - RS)")
    fim_inscricoes: str = Field(description="Data limite das inscricoes (ex: 28/10/2026) ou 'Consultar edital'")
    resumo_vaga: str = Field(description="Resumo curto com vagas, salario, carga horaria e banca.")

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
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Erro Telegram: {e}", flush=True)

def extrair_vagas():
    vagas = []
    urls_vistas = set()

    # Busca nas páginas diretas de Professor de História e Professor Geral do PCI
    urls_fontes = [
        "https://www.pciconcursos.com.br/cargos/professor-de-historia",
        "https://www.pciconcursos.com.br/pesquisa/professor-de-historia"
    ]

    for url_fonte in urls_fontes:
        try:
            resp = requests.get(url_fonte, headers=HEADERS, timeout=20)
            print(f"Fonte {url_fonte} -> Status {resp.status_code}", flush=True)
            soup = BeautifulSoup(resp.text, "html.parser")

            for div in soup.select("div.ca, div.na, li"):
                a_tag = div.find("a", href=True)
                if not a_tag:
                    continue
                href = a_tag["href"]
                if not href.startswith("http"):
                    href = "https://www.pciconcursos.com.br" + href
                if "/noticias/" in href and href not in urls_vistas:
                    titulo = a_tag.get_text(strip=True)
                    resumo_card = div.get_text(separator=" | ", strip=True)
                    if len(titulo) > 10:
                        urls_vistas.add(href)
                        vagas.append({"titulo": titulo, "url": href, "resumo_extra": resumo_card})
        except Exception as e:
            print(f"Erro ao ler {url_fonte}: {e}", flush=True)

    regex_excluir = r"\b(IF[A-Z]+|UF[A-Z]+|UTFPR|CEFET|UNIVERSIDADE|FACULDADE|GABARITO|RESULTADO|CONVOCA[ÇC][ÃA]O|HOMOLOGA[ÇC][ÃA]O|SUSPENSO|CANCELADO)\b"
    filtradas = [v for v in vagas if not re.search(regex_excluir, v["titulo"].upper())]
    print(f"Vagas diretas encontradas: {len(filtradas)}", flush=True)
    return filtradas[:15]

def analisar_vaga_com_ia(client, vaga, data_hoje):
    texto_limpo = f"Resumo do Card: {vaga['resumo_extra']}\n"
    try:
        resp = requests.get(vaga["url"], headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            conteudo = soup.select_one("div#concursos") or soup.select_one("article") or soup.body
            if conteudo:
                texto_limpo += conteudo.get_text(separator="\n", strip=True)[:14000]
    except Exception as e:
        print(f"Erro ao abrir pagina {vaga['url']}: {e}", flush=True)

    prompt = f"""
    Data de hoje: {data_hoje}.
    Analise esta pagina de concurso publico para PROFESSOR DE HISTORIA:
    Titulo: {vaga['titulo']}
    Conteudo da pagina:
    {texto_limpo}

    Regras de negocio:
    1. inscricoes_abertas = True se o prazo final de inscricao for maior ou igual a {data_hoje} (ou se abrir em breve).
    2. esfera_valida = True se for Prefeitura Municipal ou Secretaria Estadual. False se for Universidade ou Instituto Federal.
    3. aprovado_regra_jornada_salario = True se houver vaga de 40h semanais (qualquer salario), OU se o salario para Professor (ou o teto informado na noticia quando nao detalhado por cargo) for >= R$ 3.000,00.
    4. usa_pnd_como_principal = True se o edital usar a Prova Nacional Docente (PND / INEP) como etapa principal.
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json", "response_schema": AnaliseEdital, "temperature": 0.1}
    )
    return AnaliseEdital.model_validate_json(response.text)

def main():
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    historico = carregar_historico()
    salvar_historico(historico)
    vagas = extrair_vagas()

    if EVENT_NAME == "workflow_dispatch":
        enviar_telegram(
            f"🤖 <b>Radar de Concursos V2 Ativo! ({data_hoje})</b>\n"
            f"🔎 Encontrei <b>{len(vagas)}</b> editais diretos de Professor de História. Analisando um por um..."
        )

    client = genai.Client(api_key=GEMINI_API_KEY)
    aprovadas = 0
    log_descartes = []

    for v in vagas:
        if v["url"] in historico:
            continue
        try:
            print(f"Analisando: {v['titulo']}", flush=True)
            res = analisar_vaga_com_ia(client, v, data_hoje)
            historico.add(v["url"])

            if res.inscricoes_abertas and res.esfera_valida and res.aprovado_regra_jornada_salario:
                aprovadas += 1
                cabecalho = "🚨🔥 <b>ALERTA MÁXIMO: EDITAL ABERTO COM PND PRINCIPAL!</b> 🔥🚨" if res.usa_pnd_como_principal else "✅ <b>Concurso Aberto Aprovado (40h / >R$3k)</b>"
                jornada_str = f"{res.jornada_horas}h" if res.jornada_horas > 0 else "Ver edital"
                msg = (
                    f"{cabecalho}\n\n"
                    f"🏛 <b>Órgão:</b> {res.orgao}\n"
                    f"📅 <b>Inscrições até:</b> {res.fim_inscricoes}\n"
                    f"⏱ <b>Jornada:</b> {jornada_str} | 💰 <b>Salário:</b> R$ {res.salario_bruto:.2f}\n"
                    f"📋 <b>Detalhes:</b> {res.resumo_vaga}\n\n"
                    f"🔗 <a href='{v['url']}'>Acessar notícia / Edital</a>"
                )
                enviar_telegram(msg)
            else:
                motivo = f"• {res.orgao}: Aberto={res.inscricoes_abertas}, {res.jornada_horas}h, R${res.salario_bruto:.0f}"
                log_descartes.append(motivo)

            # Pausa de 5 segundos para respeitar o limite gratuito da API do Gemini!
            time.sleep(5)

        except Exception as e:
            print(f"Erro IA em {v['titulo']}: {e}", flush=True)
            time.sleep(5)

    salvar_historico(historico)

    if EVENT_NAME == "workflow_dispatch" and aprovadas == 0:
        resumo_desc = "\n".join(log_descartes[:8]) if log_descartes else "Nenhuma vaga inédita encontrada."
        enviar_telegram(
            f"🏁 <b>Varredura concluída!</b> Nenhuma vaga passou no filtro hoje.\n\n"
            f"📋 <b>Raio-X das vagas avaliadas e descartadas:</b>\n{resumo_desc}"
        )

if __name__ == "__main__":
    main()
