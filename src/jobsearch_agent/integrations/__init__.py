"""Integracoes externas opcionais.

Cada subpacote depende de bibliotecas que o nucleo NAO instala: o agente tem de
funcionar (e os testes tem de rodar) sem credencial, sem rede e sem cliente de
terceiro. Todo import de biblioteca externa aqui e TARDIO, dentro da funcao que
precisa dele.
"""
