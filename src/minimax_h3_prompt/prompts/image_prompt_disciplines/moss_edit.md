## 图生图编辑提示词纪律（MOSS-Image）

本档是 MOSS-Image **参考图编辑**的写词口径，与 `moss.md` 的文生图口径分开用。

**角色声明**：正文第一句之前先写 `This is an IMAGE EDIT, NOT A REGENERATION`——产物是唯一到达生图模型的通道，这句要进正文。

1. **正文用英文**：一整段，3–6 句。
2. **角色声明之后的第一句绑定输入图并写终点机位**：`from the input image` 加相机终点结果；不写运动过程。
3. **只写变了的东西**：机位、主体、出画/入画；没变的不重述。
4. **保留写正向**：`Keep ... unchanged`；身份指向输入图，本档没有官方编辑口径，身份、光源方向与材质三项可以点名。
5. **空间关系自洽**：说了人物走到街角，就不能说近景的车还停在身后。
6. **不写视频语言与质量套话**。
7. **背景连续只在机位不动时写**：only when the camera does not move, keep buildings, storefronts, signs, street fixtures and visible vehicles in their relative positions from the input image; when the camera moves, do not freeze the background.
8. **用编辑动词**：`Move the camera ...` / `Keep ...`, never `Create a photograph of ...` (it triggers a full repaint).
9. **位移与纵深**：state where the starting landmark now is (behind/out of frame), and give one **named landmark** with a **visible relation** to the person or the frame edge (boots stopped at the stop line, the traffic light directly ahead); do not write unresolved amounts such as "a few steps" or "several meters" — `camera now at B` alone reads as marching in place.
