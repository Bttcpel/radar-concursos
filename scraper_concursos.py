import os
import sys
import time
import json
import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from google import genai

print("=== INICIANDO ROBO DE CONCURSOS V3 ===", flush=True)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")

ARQUIVO_HISTORICO = "historico_vagas_v3.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

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

    urls_fontes = [
        "https://www.pciconcursos.com.br/cargos/professor-de-historia",
        "https://www.pciconcursos.com.br/pesquisa/professor-de-historia"
    ]

    for url_fonte in urls_fontes:
        try:
            resp = requests.get(url_fonte, headers=HEADERS, timeout=20)
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
    return filtradas[:15]

def analisar_vaga_com_ia(client, vaga, data_hoje):
    texto_limpo = f"Resumo da listagem: {vaga['resumo_extra']}\n"
    link_pdf = ""

    try:
        resp = requests.get(vaga["url"], headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            conteudo = soup.select_one("div#concursos") or soup.select_one("article") or soup.body
            if conteudo:
                texto_limpo += conteudo.get_text(separator="\n", strip=True)[:12000]
            # Procura link direto do PDF do Edital na página
            for a_pdf in soup.find_all("a", href=True):
                if ".pdf" in a_pdf["href"].lower():
                    link_pdf = a_pdf["href"]
                    break
    except Exception:
        pass

    prompt = f"""
    Data de hoje: {data_hoje}.
    Analise esta noticia de concurso publico para PROFESSOR DE HISTORIA:
    Titulo: {vaga['titulo']}
    Texto da noticia:
    {texto_limpo}

    Retorne APENAS um objeto JSON valido (sem markdown) com estas exatas chaves:
    {{
      "inscricoes_abertas": true ou false (true se as inscricoes ainda estao abertas em {data_hoje} ou abrem em breve; false se ja encerraram),
      "esfera_valida": true ou false (true se for Prefeitura Municipal, Camara ou Secretaria Estadual; false se for Universidade ou Instituto Federal),
      "jornada_horas": numero inteiro da carga horaria semanal de Professor de Historia (ex: 20, 30, 40; coloque 0 se nao informado),
      "salario_bruto": numero decimal do salario para Professor ou teto do edital (ex: 4500.50; coloque 0 se nao informado),
      "aprovado_regra": true ou false (true se jornada_horas == 40, OU se salario_bruto >= 3000, OU se nao informar salario/horas especificos de Historia mas for edital municipal/estadual aberto),
      "usa_pnd_principal": true ou false (true se mencionar Prova Nacional Docente / PND / INEP como forma de selecao),
      "orgao": "Nome da Prefeitura ou Secretaria e UF (ex: Prefeitura de Panambi - RS)",
      "fim_inscricoes": "Data final de inscricao (ex: 28/10/2026) ou Consultar edital",
      "resumo": "Resumo curto com vagas, salario, carga horaria e banca organizadora"
    }}
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"response_mime_type": "application/json", "temperature": 0.1}
    )

    texto_resp = response.text.strip()
    # Remove blocos markdown caso o modelo coloque ```json
    texto_resp = re.sub(r"^```json\s*|\s*```$", "", texto_resp, flags=re.MULTILINE).strip()
    dados = json.loads(texto_resp)
    dados["link_pdf"] = link_pdf
    return dados

def main():
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    historico = carregar_historico()
    salvar_historico(historico)
    vagas = extrair_vagas()

    if EVENT_NAME == "workflow_dispatch":
        enviar_telegram(
            f"🤖 <b>Radar de Concursos V3 Ativo! ({data_hoje})</b>\n"
            f"🔎 Encontrei <b>{len(vagas)}</b> editais diretos de Professor de História. Analisando..."
        )

    client = genai.Client(api_key=GEMINI_API_KEY)
    aprovadas = 0
    log_descartes = []
    erros_ia = []

    for v in vagas:
        if v["url"] in historico:
            continue
        try:
            res = analisar_vaga_com_ia(client, v, data_hoje)
            historico.add(v["url"])

            aberta = bool(res.get("inscricoes_abertas", False))
            esfera = bool(res.get("esfera_valida", False))
            aprovado = bool(res.get("aprovado_regra", False))
            pnd = bool(res.get("usa_pnd_principal", False))
            horas = int(res.get("jornada_horas", 0) or 0)
            salario = float(res.get("salario_bruto", 0.0) or 0.0)
            orgao = res.get("orgao", v["titulo"])
            fim_insc = res.get("fim_inscricoes", "Consultar")
            resumo = res.get("resumo", "")
            link_pdf = res.get("link_pdf", "")

            if aberta and esfera and aprovado:
                aprovadas += 1
                cabecalho = "🚨🔥 <b>ALERTA MÁXIMO: EDITAL ABERTO COM PND PRINCIPAL!</b> 🔥🚨" if pnd else "✅ <b>Concurso Aberto Aprovado (40h / >R$3k)</b>"
                jornada_str = f"{horas}h" if horas > 0 else "Ver edital"
                salario_str = f"R$ {salario:.2f}" if salario > 0 else "Ver edital"
                extra_pdf = f"\n📄 <a href='{link_pdf}'>Baixar PDF do Edital</a>" if link_pdf else ""

                msg = (
                    f"{cabecalho}\n\n"
                    f"🏛 <b>Órgão:</b> {orgao}\n"
                    f"📅 <b>Inscrições até:</b> {fim_insc}\n"
                    f"⏱ <b>Jornada:</b> {jornada_str} | 💰 <b>Salário:</b> {salario_str}\n"
                    f"📋 <b>Detalhes:</b> {resumo}\n\n"
                    f"🔗 <a href='{v['url']}'>Acessar página da vaga</a>{extra_pdf}"
                )
                enviar_telegram(msg)
            else:
                log_descartes.append(f"• {orgao}: Aberto={aberta}, {horas}h, R${salario:.0f}")

            time.sleep(4)

        except Exception as e:
            erro_curto = str(e)[:120]
            print(f"Erro IA em {v['titulo']}: {erro_curto}", flush=True)
            erros_ia.append(f"• {v['titulo'][:25]}: {erro_curto}")
            time.sleep(4)

    salvar_historico(historico)

    if EVENT_NAME == "workflow_dispatch" and aprovadas == 0:
        texto_final = f"🏁 <b>Varredura concluída!</b> Nenhuma vaga passou no filtro hoje.\n\n"
        if log_descartes:
            texto_final += "📋 <b>Avaliadas e descartadas:</b>\n" + "\n".join(log_descartes[:8]) + "\n\n"
        if erros_ia:
            texto_final += "⚠️ <b>Aviso técnico da API:</b>\n" + "\n".join(erros_ia[:3])
        enviar_telegram(texto_final)

if __name__ == "__main__":
    main()
