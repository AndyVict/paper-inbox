# 文献整理后，你会读到什么

以下两条来自已有本地消息预览，保留中文标题、英文标题、期刊、JIF/JCI 分区、中文摘要和论文链接。使用与正式推送相同的消息模板；这是本地消息预览。论文及原有机器译文按 CC BY 4.0 署名展示。期刊指标沿用消息生成时的 2025 年本地快照，尚未核验为官方最新数据；离线期刊查询可使用项目随包快照。

## 1. 使用RGB多光谱树冠特征和机器学习基于无人机估计多个飞行高度的向日葵叶黄酮醇含量。

**中文标题：**使用RGB多光谱树冠特征和机器学习基于无人机估计多个飞行高度的向日葵叶黄酮醇含量。

**英文标题：**UAV-based estimation of sunflower leaf flavonol content across multiple flight heights using RGB-multispectral canopy features and machine learning.

**期刊名称：**Front Plant Sci（JIF=5.9）；2025 年指标

**JIF分区：**PLANT SCIENCES（SCIE） / Q1

**JCI 分区：**PLANT SCIENCES（SCIE） / Q1

**中文摘要：**快速、无损地估计叶片黄酮醇含量（Flav）对于评估作物生理状态和胁迫响应非常重要。在这项研究中，基于无人机（UAV）的红色、绿色和蓝色（RGB）和多光谱图像用于估计向日葵幼苗、萌芽、开花和成熟阶段的叶片Flav。数据是在2025年生长季节在中国黄河三角洲的一个实验点收集的。在30米、50米和80米的飞行高度采集无人机图像，并使用MPM-100植物多光谱颜料仪在同一采集窗口内获得Flav的地面测量。从80个采样地块中总共获得了320个地块水平的观察值，这些地块在地块水平上被分为56个训练地块和24个测试地块，来自同一地块的所有四个生长阶段观察值被分配到同一子集。使用训练数据进行特征选择和模型开发，训练数据被进一步划分用于超参数优化，而测试集用于比较性能评估。从向日葵树冠图像中提取了总共44个无人机衍生的特征，包括22个植被指数、16个纹理特征和6个颜色特征。皮尔逊相关系数（PCC）分析和Boruta算法相结合来识别紧凑的特征子集，并评估了六个回归模型，包括K-最近邻、随机森林、CatBoost、支持向量回归、LightGBM和Transformer回归。进一步使用遗传算法（GA）、粒子群优化（PSO）和灰狼优化器（GWO）来调整中间飞行高度50 m的Transformer模型。PCC-Boruta策略降低了特征维数，并在测试集上产生了良好的预测性能。在评估的回归模型中，Transformer模型取得了最好的整体性能。使用最佳特征组合，PCC-博鲁塔-Transformer模型在30 m、50 m和80 m处分别获得了0.932、0.911和0.909的测试集R2值，相应的均方根误差（RMSE）值为0.060、0.069和0.070。因此，在三个未优化的飞行高度模型中，30 m模型提供了最高的预测精度。50米数据集代表三个评估的飞行高度中的中间水平，用于后续的超参数优化。在评估的优化配置中，GWO调谐变压器实现了最佳观察到的测试集性能，R2为0.947，RMSE为0.053。这些结果表明了在本实验的特定地点、生长季节、传感器和管理条件下评估的工作流程的可行性。

**链接：**[打开论文](https://doi.org/10.3389/fpls.2026.1931472)

© 2026 Han, Zhao, Song, Zhang, Lu and Yuan. Frontiers in Plant Science 17, 1931472. · CC-BY-4.0；中文标题与摘要为已有机器译文，未另作摘要概述。

[论文来源](https://www.frontiersin.org/journals/plant-science/articles/10.3389/fpls.2026.1931472/full) · [许可](https://creativecommons.org/licenses/by/4.0/)

## 2. 临界氮稀释曲线与高光谱特征优化和机器学习耦合反演温室黄瓜氮营养指数。

**中文标题：**临界氮稀释曲线与高光谱特征优化和机器学习耦合反演温室黄瓜氮营养指数。

**英文标题：**Coupling critical nitrogen dilution curve with hyperspectral feature optimization and machine learning for inversion of nitrogen nutrition index in greenhouse cucumber.

**期刊名称：**Front Plant Sci（JIF=5.9）；2025 年指标

**JIF分区：**PLANT SCIENCES（SCIE） / Q1

**JCI 分区：**PLANT SCIENCES（SCIE） / Q1

**中文摘要：**导言：快速、无损和准确地诊断温室黄瓜生产中的氮（N）营养状况对于减轻过度施肥风险和支持可持续集约化至关重要。
方法：通过将农艺临界氮（Nc）稀释曲线与混合竞争自适应重新加权采样和方差膨胀因子（CARS-VIF）特征选择算法耦合，开发了一个集成诊断框架，然后对六个机器学习回归模型进行基准测试，包括K-最近邻（KNN）、多层感知器（MLP）、支持向量回归（SVR）、额外树、随机森林（RF）和梯度增强回归（GBR）。
结果：临界氮稀释曲线参数化为Nc=4.378×DW-0.115（R²=0.745），经验氮营养指数（NNI）值范围为0.72至1.22。CARS-VIF管道将整个光谱压缩到四个敏感波段（430、677、688和953 nm），实现了99.81%的降维。GBR 成为最佳反演模型，验证 R² 为 0.845，RMSE 为 0.061，最低 MAE 为 0.046。
讨论：这种非破坏性框架有望在未来集成到自动化温室传感器平台或无人机（UAV）中，提供强大的数字工具包来支持精准施肥决策和可持续智能设施园艺。在业务部署之前，需要在现场条件下进行额外的验证。

**链接：**[打开论文](https://doi.org/10.3389/fpls.2026.1909263)

© 2026 Zhao, Li, Hu, Zhang, Zhang, Xu and Xiu. Frontiers in Plant Science 17, 1909263. · CC-BY-4.0；中文标题与摘要为已有机器译文，未另作摘要概述。

[论文来源](https://www.frontiersin.org/journals/plant-science/articles/10.3389/fpls.2026.1909263/full) · [许可](https://creativecommons.org/licenses/by/4.0/)
