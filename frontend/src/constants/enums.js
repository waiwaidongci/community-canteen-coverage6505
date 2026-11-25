export const ROLE = {admin:'复核主管', analyst:'分析员', viewer:'只读查看'};
export const STATUS = {queued:'排队中', running:'正在计算', complete:'计算完成', failed:'运行失败', superseded:'已被覆盖'};
export const REVIEW = {pending:'待复核', accepted:'已通过', returned:'已退回'};
export const DISTRIBUTION = {
  pending:{label:'计算中', tone:'pending'},
  failed:{label:'计算失败', tone:'failed'},
  superseded:{label:'已被覆盖', tone:'superseded'},
  stale:{label:'资格失效', tone:'stale'},
  blocked:{label:'暂缓分发', tone:'blocked'},
  eligible:{label:'可分发', tone:'eligible'},
  delivered:{label:'已分发', tone:'delivered'},
  replay:{label:'按存档复算', tone:'replay'},
};
