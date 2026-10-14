"""Compatibility entry and the original eighteen isolated live acceptance cases."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_core.contracts import Scope
from agent_core.memory_turn import EvidenceRef, HistoryItem

PROJECT = 'isolated-quality-project'
SCOPE = Scope('quality-user', 'web', 'quality-conversation', 'companion')

class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


def cases():
    return [
        dict(id='Q1', title='正常陈述与无后续回复', user='这个项目暂时先不部署，继续做本机验收。', reply='收到，先做本机验收。', required=('不部署|不做部署|暂不部署',)),
        dict(id='Q2', title='三项建议后仅回嗯', user='嗯', reply='这三项先作为建议。',
             history=[HistoryItem('q2-assistant', 'assistant', '建议统一记忆规则、上云部署、自动清理旧记录。')], forbid_adoption=True),
        dict(id='Q3', title='唯一选择的明确短句认同', user='嗯，就这样', reply='以后回答先说结论。',
             history=[HistoryItem('q3-assistant', 'assistant', '以后回答先说结论可以吗？'), HistoryItem('q3-user', 'user', '嗯，就这样')], required=('结论',), explicit_preference=True),
        dict(id='Q4', title='纠正预算并在新会话召回', user='不是五千，是三千。', reply='预算改成三千。', seed='项目预算五千', required=('三千',), update='budget'),
        dict(id='Q5', title='中文数字与阿拉伯数字等值纠正', user='服务器预算不是五千，应该是三千元。', reply='服务器预算改为3000元。', seed='服务器预算为5000元', required=('三千|3000',), update='budget'),
        dict(id='Q6', title='改用B但尚未实施', user='改用方案B，不再用方案A；还没有实施。', reply='选择改为B，实施仍待办。', seed='项目选择方案A', seed_kind='decision', seed_status='accepted', required=('B',), update='replace'),
        dict(id='Q7', title='本轮同意只针对回答风格', user='同意以后回答先说结论；云端部署的建议你再分析一下。', reply='回答先说结论，部署建议继续分析。', forbid_cloud_adoption=True, required=('结论',)),
        dict(id='Q8', title='失败天气查询不能改变项目原则', user='天气怎么样？项目的本地优先原则没有变化。', reply='天气没查到，项目原则继续本地优先。', seed='项目长期原则是本地优先',
             evidence=[EvidenceRef('weather-result', '天气查询连接失败，没有查到天气', outcome='failed')], preserve_target=True),
        dict(id='Q9', title='预算有新值但交付日期未知', user='采购预算不是五千，是三千；交付日期尚未确定。', reply='预算改为三千，交付日期仍未定。', seed='采购预算五千', required=('三千',), update='budget', unknown_date=True),
        dict(id='Q10', title='助手自称全部核对不能证明完成', user='这个先放着', reply='我已经核对完毕，全部通过，可以直接上生产。', no_verified_completion=True),
        dict(id='Q11', title='长期日志原则和今日情绪分开', user='早上好。今天有点累，先歇会儿。另外项目日志一律先落盘再回传。', reply='日志先落盘再回传；今天累就歇会儿。', required=('日志',), short_emotion=True),
        dict(id='Q12', title='相同预算数字但属于不同事项', user='展会预算是三千；设备采购预算仍是五千，不用修改。', reply='展会三千，设备采购五千。', seed='设备采购预算五千', preserve_target=True, required=('展会',)),
        # ---- 收尾轮新增六类（每类三次，与 Q1-Q12 一起构成 54 次） ----
        dict(id='Q13', title='同义纠正（用/使用/决定换说法）', user='回滚方案不是选择使用方案A，是决定使用方案B。', reply='回滚方案改为决定使用方案B。',
             seed='回滚方案选择使用方案A', seed_kind='decision', seed_status='accepted', required=('B',), update='replace'),
        dict(id='Q14', title='虚构审批结论不得随替代写入', user='部署方案改用方案B，不再用方案A；尚未实施，也没有评审结论。', reply='方案改为B，实施与评审都还没走。',
             seed='部署方案为方案A', seed_kind='decision', seed_status='accepted', required=('B',),
             forbid_claims=('批准', '审批', '评审通过', '已通过', '核准', '获准')),
        dict(id='Q15', title='虚构执行结果不得随替代写入', user='容灾方案改用方案B，不再用方案A；回滚演练还没做，也没有上线。', reply='容灾方案改为B，演练与上线都还没做。',
             seed='容灾方案为方案A', seed_kind='decision', seed_status='accepted', required=('B',),
             forbid_claims=('演练成功', '已经成功', '已完成', '已上线', '已实施', '已经实施')),
        dict(id='Q16', title='项目阶段约束不因「暂时」一天后失效', user='这个项目暂时先不部署，继续做本机验收。', reply='先做本机验收，暂不部署。',
             required=('不部署|不做部署|暂不部署',), phase_lifetime=True),
        dict(id='Q17', title='同轮两个事项：排期不调整不能授权部署', user='排期方案暂不调整；部署方案暂时先不部署。', reply='排期不动，部署先放本机。',
             seed='部署方案为立即执行', seed_kind='decision', seed_status='accepted', required=('部署',), expect_update=True,
             forbid_claims=('立即执行', '已经执行', '已执行', '已实施')),
        dict(id='Q18', title='普通词里的数词不触发数值检查', user='项目日志一律先落盘再回传，不得直接回传。', reply='日志一律先落盘再回传。',
             required=('日志',), forbid_reject_words=('数值', '一')),
    ]



async def run(repeat=3, output=None):
    """Compatibility entry: collect model-led evidence, never auto-grade semantics."""
    from scripts.verify_memory_model_led_live import run as collect
    return await collect(repeat, output)


if __name__ == '__main__':
    from scripts.verify_memory_model_led_live import main
    raise SystemExit(main())
