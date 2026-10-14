import os
import json
import re
import requests
from bs4 import BeautifulSoup
from google import genai
from pydantic import BaseModel, Field

# Configuração das chaves (vêm dos Secrets do GitHub Actions)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

URL_ALVO = "https://www.pciconcursos.com.br/cargos/professor-de-historia"
ARQUIVO_HISTORICO = "historico_vagas.json"

# Estrutura de resposta forçada (Structured Outputs) para o Gemini avaliar a vaga
class AnaliseEdital(BaseModel):
    esfera_valida: bool = Field(description="True APENAS se for Prefeitura Municipal ou Secretaria Estadual (SEDUC/SED). False se for IF, UF, Universidade ou Militar.")
    jornada_horas: int = Field(description="Carga horária semanal para Professor de História (ex: 20, 30, 40). Se houver ambas, priorize 40.")
    salario_bruto: float = Field(description="Vencimento básico bruto específico para o cargo de Professor de História em R$.")
    aprovado_regra_jornada_salario: bool = Field(
        description="True se jornada_horas == 40, OU se jornada_horas < 40 e salario_bruto >= 3000.0. Caso contrário, False."
    )
    usa_pnd_como_principal: bool = Field(
        description="True SE E SOMENTE SE o concurso utilizar a Prova Nacional Docente (PND / CNU Professores / INEP) como fase principal ou nota classificatória."
    )
    orgao: str = Field(description="Nome da Prefeitura ou Secretaria Estadual e UF (ex: Prefeitura de Pelotas - RS)")
    resumo_vaga: str = Field(description="Resumo curto com vagas, salário exato, carga horária, banca e data de inscrição.")

def carregar_historico():
    if os.path.exists(ARQUIVO_HISTORICO):
        with open(ARQUIVO_HISTORICO, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def salvar_historico(historico):
    with open(ARQUIVO_HISTORICO, "w", encoding="utf-8") as f:
        json.dump(list(historico), f, ensure_ascii=False, indent=2)

def enviar_telegram(mensagem):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(mensagem)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensagem, "parse_mode": "HTML"}
    requests.post(url, json=payload, timeout=15)

def extrair_vagas_pci():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp = requests.get(URL_ALVO, headers=headers, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")
    
    vagas = []
    # Captura os cards de concursos ativos na página de Professor de História
    for div in soup.select("div.ca"):
        link_tag = div.find("a")
        if not link_tag:
            continue
        titulo = link_tag.text.strip()
        url = link_tag["href"]
        
        # Pré-filtro rápido por Regex: já descarta Universidades e Institutos Federais pelo título
        if re.search(r"\b(IF[A-Z]+|UF[A-Z]+|UTFPR|CEFET|UNIVERSIDADE|FACULDADE)\b", titulo.upper()):
            continue
            
        vagas.append({"titulo": titulo, "url": url})
    return vagas

def analisar_vaga_com_ia(client, titulo, url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp = requests.get(url, headers=headers, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")
    
    # Extrai o texto completo da notícia da vaga (onde ficam os detalhes de horas, salários e PND)
    conteudo = soup.select_one("div#concursos") or soup.body
    texto_limpo = conteudo.get_text(separator="\n", strip=True)[:15000]
    
    prompt = f"""
    Analise os dados deste edital de concurso público para o cargo de PROFESSOR DE HISTÓRIA.
    Título: {titulo}
    Texto extraído:
    {texto_limpo}
    
    Regras estritas de negócio:
    1. A vaga DEVE ser Municipal (Prefeitura) ou Estadual (Secretaria de Educação). Rejeite Universidades e Institutos Federais.
    2. A vaga é aprovada em dois cenários:
       - Cenário A: Carga horária de 40h semanais (qualquer salário).
       - Cenário B: Carga horária de 20h (ou inferior a 40h), DESDE QUE o salário bruto para Professor seja > R$ 3.000,00.
       (Nota: se o texto não informar se é 20h ou 40h mas o salário for > R$ 3.000, aprove para verificação).
    3. Verifique se o edital utiliza a PND (Prova Nacional Docente / INEP) como nota principal de seleção.
       IMPORTANTE: Não descarte se não usar PND! A PND serve apenas para dar destaque máximo.
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
    client = genai.Client(api_key=GEMINI_API_KEY)
    historico = carregar_historico()
    vagas = extrair_vagas_pci()
    novas_processadas = 0

    for v in vagas:
        if v["url"] in historico:
            continue
            
        try:
            resultado = analisar_vaga_com_ia(client, v["titulo"], v["url"])
            historico.add(v["url"])
            novas_processadas += 1
            
            # Só notifica se for Municipal/Estadual E cumprir a regra (40h OU 20h > R$ 3k)
            if resultado.esfera_valida and resultado.aprovado_regra_jornada_salario:
                if resultado.usa_pnd_como_principal:
                    cabecalho = "🚨🔥 <b>ALERTA MÁXIMO: USA PND COMO NOTA PRINCIPAL!</b> 🔥🚨"
                else:
                    cabecalho = "✅ <b>Concurso Aprovado no Filtro (40h / >R$3k)</b>"
                    
                msg = (
                    f"{cabecalho}\n\n"
                    f"🏛 <b>Órgão:</b> {resultado.orgao}\n"
                    f"⏱ <b>Jornada:</b> {resultado.jornada_horas}h | 💰 <b>Salário:</b> R$ {resultado.salario_bruto:.2f}\n"
                    f"📋 <b>Detalhes:</b> {resultado.resumo_vaga}\n\n"
                    f"🔗 <a href='{v['url']}'>Acessar página do Edital</a>"
                )
                enviar_telegram(msg)
        except Exception as e:
            print(f"Erro ao processar {v['url']}: {e}")

    if novas_processadas > 0:
        salvar_historico(historico)

if __name__ == "__main__":
    main()
